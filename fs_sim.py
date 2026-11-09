#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simulador de Sistema de Arquivos (FS) - ETAPA 1
------------------------------------------------
- Hierarquia em árvore (caminhos absolutos e relativos, ".", "..")
- CRUD de diretórios e arquivos
- Disco simulado em blocos de tamanho fixo
- 3 estratégias de alocação: contigua | encadeada (FAT) | indexada (i-node)
- Diagnóstico: mapa gráfico do disco, fragmentação interna/externa,
  indicação de necessidade de compactação e comando de compactação (defrag)

Uso:  python fs_sim.py            (modo interativo)
      python fs_sim.py < cmds.txt (executa um roteiro de comandos)
      Dentro do simulador, digite 'help' ou 'demo'.
"""
import math
import shlex
import sys

KB = 1024
STRATEGIES = {
    "contigua": "Contígua",
    "encadeada": "Encadeada (Linked List / FAT)",
    "indexada": "Indexada (i-node)",
}


class FSError(Exception):
    pass


# ----------------------------------------------------------------- utilidades
def parse_size(text):
    s = str(text).strip().upper().replace(" ", "")
    for unit, mult in (("MB", KB * KB), ("KB", KB), ("M", KB * KB), ("K", KB), ("B", 1)):
        if s.endswith(unit):
            s = s[: -len(unit)]
            break
    else:
        mult = 1
    try:
        value = int(float(s) * mult)
    except ValueError:
        raise FSError(f"Tamanho inválido: '{text}' (use ex.: 512B, 4KB, 10KB)")
    if value < 0:
        raise FSError("O tamanho não pode ser negativo.")
    return value


def fmt_size(n):
    if n >= KB:
        return f"{n / KB:g} KB"
    return f"{n} B"


def parse_args(tokens, flags=()):
    """Separa argumentos posicionais, opções (--chave valor) e flags (-r, --details)."""
    pos, opts, i = [], {}, 0
    while i < len(tokens):
        t = tokens[i]
        key = t.lstrip("-")
        if t.startswith("-") and key in flags:
            opts[key] = True
        elif t.startswith("--"):
            if i + 1 >= len(tokens):
                raise FSError(f"Faltou o valor da opção {t}")
            opts[key] = tokens[i + 1]
            i += 1
        else:
            pos.append(t)
        i += 1
    return pos, opts


# -------------------------------------------------------------- estruturas
class Node:
    def __init__(self, name, parent):
        self.name = name
        self.parent = parent
        self.inode = None  # bloco do i-node (apenas na alocação indexada)

    def path(self):
        if self.parent is None:
            return "/"
        base = self.parent.path()
        return (base if base.endswith("/") else base + "/") + self.name


class Directory(Node):
    def __init__(self, name, parent):
        super().__init__(name, parent)
        self.children = {}


class File(Node):
    def __init__(self, name, parent):
        super().__init__(name, parent)
        self.size = 0
        self.content = None  # texto real (opcional); se None, conteúdo é simulado
        self.blocks = []     # blocos de dados, em ordem lógica


class Disk:
    """Camada física: vetor de blocos de tamanho fixo + tabela FAT."""

    def __init__(self, block_size, total_blocks):
        self.bs = block_size
        self.n = total_blocks
        self.blocks = [None] * total_blocks  # None = livre | dict com dados do bloco
        self.fat = [None] * total_blocks     # próximo bloco (encadeada); -1 = fim

    def free_list(self):
        return [i for i, b in enumerate(self.blocks) if b is None]

    def holes(self):
        """Lista de lacunas livres: (início, tamanho)."""
        holes, start = [], None
        for i, b in enumerate(self.blocks + ["fim"]):
            if b is None and start is None:
                start = i
            elif b is not None and start is not None:
                holes.append((start, i - start))
                start = None
        return holes

    def first_fit(self, n):
        for start, size in self.holes():
            if size >= n:
                return start
        return None


# --------------------------------------------------------- sistema de arquivos
class FileSystem:
    def __init__(self, block_size=4 * KB, total_blocks=16, strategy="indexada"):
        if strategy not in STRATEGIES:
            raise FSError(f"Estratégia inválida. Use: {', '.join(STRATEGIES)}")
        if block_size <= 0 or total_blocks <= 0:
            raise FSError("Tamanho de bloco e total de blocos devem ser maiores que zero.")
        self.disk = Disk(block_size, total_blocks)
        self.strategy = strategy
        self.root = Directory("", None)
        self.cwd = self.root
        if strategy == "indexada":
            self.disk.blocks[0] = {"kind": "INODE_RAIZ", "owner": self.root}
            self.root.inode = 0

    # ------------------------------------------------------ caminhos
    def resolve(self, path):
        if not path:
            return self.cwd
        node = self.root if path.startswith("/") else self.cwd
        for part in path.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                node = node.parent or node
                continue
            if not isinstance(node, Directory) or part not in node.children:
                return None
            node = node.children[part]
        return node

    def must_resolve(self, path):
        node = self.resolve(path)
        if node is None:
            raise FSError(f"Caminho não encontrado: {path}")
        return node

    def parent_and_name(self, path):
        clean = path.rstrip("/")
        if clean == "":
            raise FSError("Operação inválida sobre a raiz '/'.")
        if "/" in clean:
            head, name = clean.rsplit("/", 1)
            head = head or "/"
        else:
            head, name = ".", clean
        if name in (".", ".."):
            raise FSError(f"Nome inválido: {name}")
        parent = self.resolve(head)
        if not isinstance(parent, Directory):
            raise FSError(f"Diretório pai não existe: {head}")
        return parent, name

    # ------------------------------------------------------ alocação
    def _allocate(self, f, size):
        d = self.disk
        n = math.ceil(size / d.bs)
        free = d.free_list()

        if self.strategy == "contigua":
            if n == 0:
                blocks = []
            else:
                start = d.first_fit(n)
                if start is None:
                    if len(free) >= n:
                        raise FSError(
                            f"FRAGMENTAÇÃO EXTERNA: o arquivo precisa de {n} blocos seguidos. "
                            f"Existem {len(free)} blocos livres, mas espalhados "
                            f"(maior lacuna = {max(s for _, s in d.holes())} blocos). "
                            "Use 'defrag' para compactar o disco.")
                    raise FSError(f"Disco cheio: precisa de {n} blocos, há {len(free)} livres.")
                blocks = list(range(start, start + n))

        elif self.strategy == "encadeada":
            if len(free) < n:
                raise FSError(f"Disco cheio: precisa de {n} blocos, há {len(free)} livres.")
            blocks = free[:n]
            for i, b in enumerate(blocks):
                d.fat[b] = blocks[i + 1] if i + 1 < n else -1

        else:  # indexada: 1 bloco de i-node + n blocos de dados
            if len(free) < n + 1:
                raise FSError(f"Disco cheio: precisa de {n + 1} blocos (1 i-node + {n} dados), "
                              f"há {len(free)} livres.")
            f.inode = free[0]
            d.blocks[f.inode] = {"kind": "INODE", "owner": f}
            blocks = free[1:n + 1]

        for seq, b in enumerate(blocks):
            used = min(d.bs, size - seq * d.bs)
            d.blocks[b] = {"kind": "DADO", "owner": f, "seq": seq + 1, "used": used}
        f.blocks = blocks
        f.size = size

    def _release(self, f):
        d = self.disk
        for b in f.blocks:
            d.blocks[b] = None
            d.fat[b] = None
        if f.inode is not None:
            d.blocks[f.inode] = None
            f.inode = None
        f.blocks = []

    def _reallocate(self, f, new_size):
        """Libera e aloca de novo; se falhar, restaura o estado anterior."""
        d = self.disk
        snap = (list(d.blocks), list(d.fat), list(f.blocks), f.inode, f.size)
        self._release(f)
        try:
            self._allocate(f, new_size)
        except FSError:
            d.blocks, d.fat, f.blocks, f.inode, f.size = snap
            raise

    # ------------------------------------------------------ CRUD
    def mkdir(self, path, parents=False):
        if parents:
            node = self.root if path.startswith("/") else self.cwd
            for part in [p for p in path.split("/") if p not in ("", ".")]:
                if part == "..":
                    node = node.parent or node
                    continue
                if part not in node.children:
                    node.children[part] = Directory(part, node)
                node = node.children[part]
                if not isinstance(node, Directory):
                    raise FSError(f"'{node.path()}' é um arquivo.")
            return
        parent, name = self.parent_and_name(path)
        if name in parent.children:
            raise FSError(f"Já existe: {path}")
        parent.children[name] = Directory(name, parent)

    def create(self, path, size=0, content=None):
        parent, name = self.parent_and_name(path)
        if name in parent.children:
            raise FSError(f"Já existe: {path}")
        f = File(name, parent)
        if content is not None:
            f.content = content
            size = len(content.encode("utf-8"))
        self._allocate(f, size)
        parent.children[name] = f
        return f

    def read(self, path):
        f = self.must_resolve(path)
        if not isinstance(f, File):
            raise FSError(f"'{path}' é um diretório (use ls).")
        if f.content is not None:
            return f.content
        return f"[conteúdo simulado de {f.size} bytes]"

    def write(self, path, size=None, content=None, append=False):
        f = self.must_resolve(path)
        if not isinstance(f, File):
            raise FSError(f"'{path}' é um diretório.")
        if content is not None and append and f.content is None:
            # arquivo com conteúdo simulado: apenas cresce de tamanho
            self._reallocate(f, f.size + len(content.encode("utf-8")))
        elif content is not None:
            new_text = (f.content or "") + content if append else content
            self._reallocate(f, len(new_text.encode("utf-8")))
            f.content = new_text
        elif size is not None:
            self._reallocate(f, size)
            f.content = None
        else:
            raise FSError("Informe --content \"texto\" ou --size N.")

    def move(self, src, dst):
        node = self.must_resolve(src)
        if node is self.root:
            raise FSError("Não é possível mover a raiz.")
        target = self.resolve(dst)
        if isinstance(target, Directory):
            parent, name = target, node.name
        else:
            parent, name = self.parent_and_name(dst)
        if name in parent.children:
            raise FSError(f"Já existe: {parent.path()}/{name}")
        p = parent
        while p is not None:
            if p is node:
                raise FSError("Não é possível mover um diretório para dentro dele mesmo.")
            p = p.parent
        del node.parent.children[node.name]
        node.name, node.parent = name, parent
        parent.children[name] = node

    def delete(self, path, recursive=False, only_dir=False):
        node = self.must_resolve(path)
        if node is self.root:
            raise FSError("Não é possível remover a raiz.")
        if isinstance(node, Directory):
            if not only_dir and not recursive:
                raise FSError(f"'{path}' é um diretório (use rmdir ou rm -r).")
            if node.children and not recursive:
                raise FSError(f"Diretório não está vazio: {path} (use -r).")
            self._release_tree(node)
            c = self.cwd
            while c is not None:  # se o cwd estava dentro, volta para o pai
                if c is node:
                    self.cwd = node.parent
                    break
                c = c.parent
        else:
            if only_dir:
                raise FSError(f"'{path}' é um arquivo (use rm).")
            self._release(node)
        del node.parent.children[node.name]

    def _release_tree(self, d):
        for child in d.children.values():
            if isinstance(child, File):
                self._release(child)
            else:
                self._release_tree(child)

    # ------------------------------------------------------ compactação
    def defrag(self):
        d = self.disk
        mapping, new_blocks, new_fat = {}, [None] * d.n, [None] * d.n
        nxt = 0
        for i, b in enumerate(d.blocks):
            if b is not None:
                mapping[i] = nxt
                new_blocks[nxt] = b
                nxt += 1
        moved = sum(1 for o, n in mapping.items() if o != n)
        for old, new in mapping.items():
            if d.fat[old] is not None:
                new_fat[new] = -1 if d.fat[old] == -1 else mapping[d.fat[old]]
        for f in self.all_files():
            f.blocks = [mapping[b] for b in f.blocks]
            if f.inode is not None:
                f.inode = mapping[f.inode]
        if self.root.inode is not None:
            self.root.inode = mapping[self.root.inode]
        d.blocks, d.fat = new_blocks, new_fat
        return moved

    # ------------------------------------------------------ consultas
    def all_files(self, d=None):
        d = d or self.root
        out = []
        for child in d.children.values():
            out.extend([child] if isinstance(child, File) else self.all_files(child))
        return out

    def tree(self, path="/"):
        start = self.must_resolve(path)
        lines = ["[ÁRVORE DO SISTEMA DE ARQUIVOS]", start.path()]

        def walk(d, prefix):
            items = sorted(d.children.values(), key=lambda n: (isinstance(n, File), n.name))
            for i, n in enumerate(items):
                last = i == len(items) - 1
                branch = "└── " if last else "├── "
                if isinstance(n, Directory):
                    lines.append(f"{prefix}{branch}{n.name}/ [DIR]")
                    walk(n, prefix + ("    " if last else "│   "))
                else:
                    lines.append(f"{prefix}{branch}{n.name} ({fmt_size(n.size)}) [ARQ]")

        if isinstance(start, Directory):
            walk(start, "")
        return "\n".join(lines)

    def ls(self, path=""):
        node = self.must_resolve(path)
        if isinstance(node, File):
            return f"{node.name}  ({fmt_size(node.size)})"
        if not node.children:
            return "(vazio)"
        rows = []
        for n in sorted(node.children.values(), key=lambda n: (isinstance(n, File), n.name)):
            rows.append(f"  {n.name}/" if isinstance(n, Directory)
                        else f"  {n.name:<20} {fmt_size(n.size):>10}")
        return "\n".join(rows)

    def stat(self, path):
        n = self.must_resolve(path)
        tipo = "Diretório" if isinstance(n, Directory) else "Arquivo"
        out = [f"Caminho : {n.path()}", f"Tipo    : {tipo}"]
        if isinstance(n, File):
            out.append(f"Tamanho : {n.size} bytes ({fmt_size(n.size)})")
            if n.inode is not None:
                out.append(f"i-node  : bloco {n.inode:02d} -> ponteiros {n.blocks}")
            if self.strategy == "encadeada" and n.blocks:
                out.append("Cadeia  : " + " -> ".join(f"{b:02d}" for b in n.blocks) + " -> FIM")
            if self.strategy == "contigua" and n.blocks:
                out.append(f"Início  : bloco {n.blocks[0]:02d} | Tamanho: {len(n.blocks)} blocos")
            out.append(f"Blocos  : {[f'{b:02d}' for b in n.blocks]}")
        else:
            out.append(f"Itens   : {len(n.children)}")
        return "\n".join(out)

    # ------------------------------------------------------ diagnóstico
    def status(self, details=False):
        d = self.disk
        files = sorted(self.all_files(), key=lambda f: f.path())
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        tag = {f: letters[i % len(letters)] for i, f in enumerate(files)}
        used = sum(1 for b in d.blocks if b is not None)
        out = [f"Tamanho do Bloco de Disco: {fmt_size(d.bs)} | Total de Blocos: {d.n} "
               f"({fmt_size(d.bs * d.n)}) | Estratégia: {STRATEGIES[self.strategy]}",
               f"Blocos ocupados: {used} | Blocos livres: {d.n - used}", "",
               "[MAPA GRÁFICO DO DISCO]  (Letra = arquivo, número = parte do arquivo, "
               "iX = i-node, R = raiz, .. = livre)"]

        per_row = 8
        for r in range(0, d.n, per_row):
            idx = " ".join(f"{i:^4}" for i in range(r, min(r + per_row, d.n)))
            cells = []
            for i in range(r, min(r + per_row, d.n)):
                b = d.blocks[i]
                if b is None:
                    c = ".."
                elif b["kind"] == "INODE_RAIZ":
                    c = "R"
                elif b["kind"] == "INODE":
                    c = "i" + tag[b["owner"]]
                else:
                    c = f"{tag[b['owner']]}{b['seq']}"
                cells.append(f"[{c:^2}]")
            out.append("  " + idx)
            out.append("  " + " ".join(cells))
        if files:
            out.append("Legenda: " + " | ".join(f"{tag[f]} = {f.path()}" for f in files))

        if details:
            out += ["", f"[MAPA DE ALOCAÇÃO DO DISCO ({STRATEGIES[self.strategy]})]"]
            i = 0
            while i < d.n:
                b = d.blocks[i]
                if b is None:
                    j = i
                    while j + 1 < d.n and d.blocks[j + 1] is None:
                        j += 1
                    label = f"Bloco {i:02d}" if i == j else f"Bloco {i:02d}..{j:02d}"
                    out.append(f"{label:<14}{'':<16}: [LIVRE]")
                    i = j + 1
                    continue
                if b["kind"] == "INODE_RAIZ":
                    out.append(f"Bloco {i:02d}      [INODE_RAIZ]  : Diretório '/'")
                elif b["kind"] == "INODE":
                    f = b["owner"]
                    ptr = ",".join(f"{x:02d}" for x in f.blocks)
                    out.append(f"Bloco {i:02d}      [INODE_{tag[f]}]     : Arquivo '{f.path()}' "
                               f"(ponteiros: {ptr or '-'})")
                else:
                    f = b["owner"]
                    extra = ""
                    if self.strategy == "encadeada":
                        nx = d.fat[i]
                        extra = " -> próximo: " + ("FIM" if nx == -1 else f"{nx:02d}")
                    out.append(f"Bloco {i:02d}      [DADO]        : '{f.path()}' (Bloco {b['seq']} - "
                               f"{b['used']} bytes ocupados){extra}")
                i += 1

        # ---- relatório de fragmentação
        out += ["", "--- RELATÓRIO DE FRAGMENTAÇÃO ---"]
        total_int = 0
        if not files:
            out.append("Nenhum arquivo no disco.")
        for f in files:
            alloc = len(f.blocks) * d.bs
            waste = alloc - f.size
            total_int += waste
            out.append(f"Tamanho Real do Arquivo '{f.name}': {f.size} bytes ({fmt_size(f.size)})")
            out.append(f"Espaço Alocado no Disco          : {alloc} bytes "
                       f"({len(f.blocks)} blocos de {fmt_size(d.bs)})")
            if waste and f.blocks:
                out.append(f"Fragmentação Interna             : {waste} bytes "
                           f"({round(waste / d.bs * 100, 1):g}% de desperdício no Bloco {f.blocks[-1]:02d})")
            else:
                out.append("Fragmentação Interna             : 0 bytes")
            out.append("")
        out.append(f"Fragmentação Interna TOTAL       : {total_int} bytes")

        holes = d.holes()
        free_total = sum(s for _, s in holes)
        if self.strategy == "contigua":
            largest = max((s for _, s in holes), default=0)
            ext_blocks = free_total - largest if len(holes) > 1 else 0
            last_used = max((i for i, b in enumerate(d.blocks) if b is not None), default=-1)
            gaps = [h for h in holes if h[0] < last_used]
            out.append(f"Lacunas livres                   : {len(holes)} "
                       f"{[f'{s:02d}..{s + t - 1:02d}' for s, t in holes]}")
            out.append(f"Maior lacuna contígua            : {largest} blocos ({largest * d.bs} bytes)")
            out.append(f"Fragmentação Externa             : {ext_blocks * d.bs} bytes "
                       f"({ext_blocks} blocos livres fora da maior lacuna)")
            if gaps:
                out.append("Necessidade de Compactação       : SIM (há espaços livres espalhados "
                           "entre arquivos; execute 'defrag')")
            else:
                out.append("Necessidade de Compactação       : NÃO (o espaço livre está todo junto)")
        else:
            motivo = ("blocos encadeados pela FAT" if self.strategy == "encadeada"
                      else "blocos alocados via i-node indexado")
            out.append("Fragmentação Externa             : 0 bytes")
            out.append(f"Necessidade de Compactação       : NÃO ({motivo}; qualquer bloco livre "
                       "pode ser usado)")
        return "\n".join(out)


# --------------------------------------------------------------------- CLI
HELP = """
COMANDOS DISPONÍVEIS
  format [--block 4KB] [--blocks 16] [--strategy indexada|contigua|encadeada]
                                   Formata o disco (apaga tudo)
  mkdir [-p] <caminho>             Cria diretório (-p cria os intermediários)
  create <caminho> [--size 10KB] [--content "texto"]
                                   Cria arquivo (C)
  cat <caminho>                    Lê arquivo (R)          | alias: read
  write <caminho> --size N | --content "texto"
                                   Atualiza arquivo (U)    | alias: update
  append <caminho> --content "texto"
                                   Acrescenta texto ao arquivo (U)
  mv <origem> <destino>            Renomeia/move arquivo ou diretório (U)
  rm [-r] <caminho>                Remove arquivo (ou diretório com -r) (D)
  rmdir [-r] <caminho>             Remove diretório (D)
  ls [caminho] | cd <caminho> | pwd | tree [caminho] | stat <caminho>
  fs_status [--details]            Mapa do disco + relatório de fragmentação
  defrag                           Compacta o disco (junta os blocos livres)
  demo                             Executa uma demonstração completa
  help | exit
Caminhos absolutos (/home/user/doc.txt) e relativos (doc.txt, ../logs) são aceitos.
"""

DEMO = [
    "format --block 4KB --blocks 16 --strategy indexada",
    "mkdir /sistema",
    "mkdir /sistema/logs",
    "create /sistema/logs/app.log --size 10KB",
    "tree",
    "fs_status --details",
    "format --block 1KB --blocks 12 --strategy contigua",
    "mkdir -p /home/user",
    "create /home/user/a.txt --size 3KB",
    "create /home/user/b.txt --size 2500B",
    "create /home/user/c.txt --size 3KB",
    "create /home/user/d.txt --size 2KB",
    "rm /home/user/b.txt",
    "fs_status",
    "create /home/user/e.txt --size 4KB",
    "defrag",
    "create /home/user/e.txt --size 4KB",
    "tree",
    "fs_status --details",
]


class Shell:
    def __init__(self):
        self.fs = FileSystem()

    def run(self, line):
        tokens = shlex.split(line)
        if not tokens:
            return ""
        cmd, args = tokens[0].lower(), tokens[1:]
        fs = self.fs

        if cmd == "help":
            return HELP
        if cmd == "format":
            _, o = parse_args(args)
            self.fs = FileSystem(parse_size(o.get("block", "4KB")),
                                 int(o.get("blocks", 16)),
                                 o.get("strategy", "indexada").lower())
            d = self.fs.disk
            return (f"Disco formatado: {d.n} blocos de {fmt_size(d.bs)} "
                    f"| Estratégia: {STRATEGIES[self.fs.strategy]}")
        if cmd == "mkdir":
            p, o = parse_args(args, flags=("p",))
            if not p:
                raise FSError("Uso: mkdir [-p] <caminho>")
            for path in p:
                fs.mkdir(path, parents=o.get("p", False))
            return ""
        if cmd in ("create", "touch"):
            p, o = parse_args(args)
            if not p:
                raise FSError("Uso: create <caminho> [--size N] [--content \"texto\"]")
            f = fs.create(p[0], parse_size(o.get("size", 0)), o.get("content"))
            return f"Arquivo criado: {f.path()} ({f.size} bytes, {len(f.blocks)} bloco(s) de dados)"
        if cmd in ("cat", "read"):
            return fs.read(args[0]) if args else "Uso: cat <caminho>"
        if cmd in ("write", "update", "append"):
            p, o = parse_args(args)
            if not p:
                raise FSError(f"Uso: {cmd} <caminho> --size N | --content \"texto\"")
            size = parse_size(o["size"]) if "size" in o else None
            fs.write(p[0], size, o.get("content"), append=(cmd == "append"))
            f = fs.resolve(p[0])
            return f"Arquivo atualizado: {f.path()} ({f.size} bytes)"
        if cmd in ("mv", "rename"):
            if len(args) != 2:
                raise FSError("Uso: mv <origem> <destino>")
            fs.move(args[0], args[1])
            return ""
        if cmd in ("rm", "rmdir"):
            p, o = parse_args(args, flags=("r",))
            if not p:
                raise FSError(f"Uso: {cmd} [-r] <caminho>")
            for path in p:
                fs.delete(path, recursive=o.get("r", False), only_dir=(cmd == "rmdir"))
            return ""
        if cmd == "ls":
            return fs.ls(args[0] if args else "")
        if cmd == "cd":
            node = fs.must_resolve(args[0] if args else "/")
            if not isinstance(node, Directory):
                raise FSError("Não é um diretório.")
            fs.cwd = node
            return ""
        if cmd == "pwd":
            return fs.cwd.path()
        if cmd == "tree":
            return fs.tree(args[0] if args else "/")
        if cmd == "stat":
            return fs.stat(args[0]) if args else "Uso: stat <caminho>"
        if cmd in ("fs_status", "status", "diskmap"):
            _, o = parse_args(args, flags=("details",))
            return fs.status(details=o.get("details", False))
        if cmd in ("defrag", "compact"):
            moved = fs.defrag()
            return f"Compactação concluída: {moved} bloco(s) movido(s). Espaço livre agora está contíguo."
        if cmd == "demo":
            for c in DEMO:
                print(f"\n> {c}")
                self.safe_run(c)
            return "\n[Fim da demonstração]"
        raise FSError(f"Comando desconhecido: {cmd} (digite 'help')")

    def safe_run(self, line):
        try:
            out = self.run(line)
            if out:
                print(out)
        except FSError as e:
            print(f"ERRO: {e}")
        except (ValueError, IndexError) as e:
            print(f"ERRO: argumentos inválidos ({e})")


def main():
    sh = Shell()
    interactive = sys.stdin.isatty()
    print("Simulador de Sistema de Arquivos - Etapa 1  (digite 'help' ou 'demo')")
    while True:
        try:
            line = input(f"{sh.fs.cwd.path()}> " if interactive else "")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not interactive:
            print(f"> {line}")
        if line.strip().lower() in ("exit", "quit", "sair"):
            break
        sh.safe_run(line)


if __name__ == "__main__":
    main()
