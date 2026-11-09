#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simulador de Entrada/Saída (E/S) - ETAPA 2
-------------------------------------------
Usa o Sistema de Arquivos da Etapa 1 (fs_sim.py) e adiciona, embaixo dele:

  Sistema de Arquivos (Etapa 1)
        |  cada leitura/gravação vira transações de E/S (bloco lógico)
        v
  Camada RAID  (none | 0 | 1 | 4 | 5)  -> distribui blocos + paridade XOR
        |
        v
  Dispositivos físicos
     - HDD: fila de requisições + escalonamento do braço
            (FCFS, SSTF, SCAN, C-SCAN, LOOK, C-LOOK)
     - SSD: FTL (mapa bloco lógico -> página física), escrita fora do lugar,
            coleta de lixo (GC) e nivelamento de desgaste (Wear Leveling)

Uso:  python io_sim.py             (modo interativo)
      python io_sim.py < roteiro   (executa um roteiro)
      Dentro do simulador: 'help', 'demo' (Etapa 1) ou 'demo2' (Etapa 2).
"""
import math
import shlex
import sys

from fs_sim import (Directory, File, FSError, Shell, STRATEGIES, HELP as HELP_FS,
                    fmt_size, parse_args, parse_size)

ALGOS = {
    "fcfs": "FCFS (First Come, First Served)",
    "sstf": "SSTF (Shortest Seek Time First)",
    "scan": "SCAN (Elevador)",
    "cscan": "C-SCAN (SCAN Circular)",
    "look": "LOOK",
    "clook": "C-LOOK",
}
RAID_LEVELS = ("none", "0", "1", "4", "5")


def xor_blocks(*blocks):
    """XOR byte a byte de vários blocos do mesmo tamanho."""
    acc = 0
    for b in blocks:
        acc ^= int.from_bytes(b, "big")
    return acc.to_bytes(len(blocks[0]), "big")


# ======================================================================
#  ESCALONAMENTO DO BRAÇO DO DISCO (HDD)
# ======================================================================
def schedule(head, tracks, algo, max_track, direction="up"):
    """Devolve a lista de paradas da cabeça: [(trilha, índice_da_requisição | None)].
    None = ponto de virada no extremo do disco (sem requisição)."""
    items = list(enumerate(tracks))
    steps = []
    if algo == "fcfs":
        steps = [(t, i) for i, t in items]
    elif algo == "sstf":
        pos, rest = head, items[:]
        while rest:
            k = min(range(len(rest)), key=lambda j: (abs(rest[j][1] - pos), rest[j][1]))
            i, t = rest.pop(k)
            steps.append((t, i))
            pos = t
    else:
        if direction == "up":
            first = sorted([x for x in items if x[1] >= head], key=lambda x: x[1])
            second = sorted([x for x in items if x[1] < head], key=lambda x: -x[1])
            end, other_end = max_track, 0
        else:
            first = sorted([x for x in items if x[1] <= head], key=lambda x: -x[1])
            second = sorted([x for x in items if x[1] > head], key=lambda x: x[1])
            end, other_end = 0, max_track
        if not first and algo in ("scan", "look"):
            # nada à frente: a cabeça já inverte o sentido (não vai ao extremo à toa)
            return schedule(head, tracks, algo, max_track, "down" if direction == "up" else "up")
        steps = [(t, i) for i, t in first]
        if second:
            last = steps[-1][0] if steps else head
            if algo == "scan":
                if last != end:
                    steps.append((end, None))
                steps += [(t, i) for i, t in second]
            elif algo == "look":
                steps += [(t, i) for i, t in second]
            elif algo == "cscan":
                if last != end:
                    steps.append((end, None))
                steps.append((other_end, None))
                steps += [(t, i) for i, t in reversed(second)]
            elif algo == "clook":
                steps += [(t, i) for i, t in reversed(second)]
    return steps


def schedule_report(head, tracks, algo, max_track, direction="up"):
    """Gera o texto no formato do enunciado e devolve (linhas, paradas, total)."""
    steps = schedule(head, tracks, algo, max_track, direction)
    path = [head] + [t for t, _ in steps]
    moves = [abs(a - b) for a, b in zip(path, path[1:])]
    total = sum(moves)
    label = [str(head)] + [f"{t}*" if i is None else str(t) for t, i in steps]
    extra = f", direção inicial: {'crescente' if direction == 'up' else 'decrescente'}" \
        if algo in ("scan", "cscan", "look", "clook") else ""
    lines = [f"--- ESCALONAMENTO DE DISCO: {ALGOS[algo]}{extra} ---",
             f"Ordem de Atendimento  : {' -> '.join(label)}"]
    if moves:
        lines.append("Deslocamento da Cabeça: " +
                     " + ".join(f"|{a}-{b}|" for a, b in zip(path, path[1:])))
        lines.append(f"Total de Movimento    : {' + '.join(map(str, moves))} = {total} trilhas")
    else:
        lines.append("Total de Movimento    : 0 trilhas")
    if any(i is None for _, i in steps):
        note = "(* = extremo do disco, a cabeça passa por ali sem atender requisição"
        if algo == "cscan":
            note += "; o retorno ao início foi somado ao total"
        lines.append(note + ")")
    return lines, steps, total


# ======================================================================
#  DISPOSITIVOS FÍSICOS
# ======================================================================
class HDD:
    kind = "HDD"

    def __init__(self, capacity, block_size, tracks=200):
        self.cap = capacity
        self.bs = block_size
        self.tracks = tracks
        self.head = min(53, tracks - 1)
        self.direction = "up"
        self.data = {}          # lba -> bytes
        self.failed = False
        self.pending = []       # requisições de outros processos (trilhas)
        self.total_moves = 0
        self.events = []

    def track_of(self, lba):
        return min(self.tracks - 1, lba * self.tracks // max(1, self.cap))

    def read(self, lba):
        return self.data.get(lba, bytes(self.bs))

    def write(self, lba, data):
        self.data[lba] = data

    def trim(self, lba):
        pass  # HDD não precisa de TRIM: o dado antigo fica lá até ser sobrescrito


class SSD:
    """SSD com FTL: mapa LBA -> (bloco de apagamento, página)."""
    kind = "SSD"
    STATIC_WL_LIMIT = 3  # diferença máxima de desgaste tolerada antes de mover dado "frio"

    def __init__(self, capacity, block_size, pages_per_block=4, wear_leveling=True):
        self.cap = capacity
        self.bs = block_size
        self.ppb = pages_per_block
        self.wl = wear_leveling
        spare = max(3, math.ceil(capacity * 0.25 / pages_per_block))  # sobre-provisionamento
        self.nblocks = math.ceil(capacity / pages_per_block) + spare
        self.state = [["F"] * self.ppb for _ in range(self.nblocks)]  # F livre, V válida, I inválida
        self.owner = [[None] * self.ppb for _ in range(self.nblocks)]
        self.pagedata = {}
        self.erase = [0] * self.nblocks
        self.map = {}
        self.active, self.next = None, 0
        self.failed = False
        self.host_writes = 0
        self.nand_writes = 0
        self.in_gc = False
        self.events = []

    # --- auxiliares
    def _erased(self):
        return [b for b in range(self.nblocks)
                if b != self.active and all(s == "F" for s in self.state[b])]

    def _free_space(self):
        room = (self.ppb - self.next) if self.active is not None else 0
        return room + len(self._erased()) * self.ppb

    def _take_block(self):
        if not self.in_gc and len(self._erased()) < 2:
            self._gc()
        erased = self._erased()
        if not erased:
            raise FSError("SSD sem páginas livres.")
        if self.wl:   # nivelamento dinâmico: usa o bloco MENOS gasto
            b = min(erased, key=lambda x: (self.erase[x], x))
        else:         # sem nivelamento: sempre o primeiro bloco livre
            b = min(erased)
        self.active, self.next = b, 0

    def _program(self, lba, data):
        if self.active is None or self.next >= self.ppb:
            self._take_block()
        b, p = self.active, self.next
        self.next += 1
        old = self.map.get(lba)
        if old:
            self.state[old[0]][old[1]] = "I"
            self.owner[old[0]][old[1]] = None
            self.pagedata.pop(old, None)
        self.state[b][p] = "V"
        self.owner[b][p] = lba
        self.pagedata[(b, p)] = data
        self.map[lba] = (b, p)
        self.nand_writes += 1
        return (b, p), old

    def _evacuate_and_erase(self, b, reason):
        moved = 0
        for p in range(self.ppb):
            if self.state[b][p] == "V":
                self._program(self.owner[b][p], self.pagedata[(b, p)])
                moved += 1
        for p in range(self.ppb):
            self.pagedata.pop((b, p), None)
        self.state[b] = ["F"] * self.ppb
        self.owner[b] = [None] * self.ppb
        self.erase[b] += 1
        self.events.append(f"{reason}: bloco físico B{b} apagado ({moved} página(s) válida(s) "
                           f"copiada(s)) -> desgaste B{b} = {self.erase[b]} apagamento(s)")

    def _gc(self):
        self.in_gc = True
        try:
            for _ in range(self.nblocks * 3):
                if len(self._erased()) >= 2:
                    break
                cands = [b for b in range(self.nblocks)
                         if b != self.active and "I" in self.state[b]]
                if not cands:
                    break
                if self.wl:
                    victim = max(cands, key=lambda b: (self.state[b].count("I"), -self.erase[b], -b))
                else:
                    victim = max(cands, key=lambda b: (self.state[b].count("I"), -b))
                if self._free_space() < self.state[victim].count("V"):
                    break
                self._evacuate_and_erase(victim, "GC (coleta de lixo)")
            if self.wl:
                self._static_wear_leveling()
        finally:
            self.in_gc = False

    def _static_wear_leveling(self):
        if max(self.erase) - min(self.erase) <= self.STATIC_WL_LIMIT:
            return
        cold = [b for b in range(self.nblocks) if b != self.active and "V" in self.state[b]]
        if not cold:
            return
        b = min(cold, key=lambda x: (self.erase[x], x))
        if max(self.erase) - self.erase[b] <= self.STATIC_WL_LIMIT:
            return
        valid = self.state[b].count("V")
        if self._free_space() - valid < self.ppb:
            return
        self._evacuate_and_erase(b, "WEAR LEVELING estático (dado frio movido)")

    # --- interface do dispositivo
    def read(self, lba):
        loc = self.map.get(lba)
        if loc is None:
            return bytes(self.bs)
        self.events.append(f"Leitura LBA {lba:02d} <- página física B{loc[0]}:P{loc[1]} "
                           "(acesso direto, sem braço mecânico)")
        return self.pagedata[loc]

    def write(self, lba, data):
        self.host_writes += 1
        (b, p), old = self._program(lba, data)
        msg = f"Escrita LBA {lba:02d} -> página física B{b}:P{p}"
        if old:
            msg += f" (página antiga B{old[0]}:P{old[1]} marcada como INVÁLIDA - escrita fora do lugar)"
        self.events.append(msg)

    def trim(self, lba):
        old = self.map.pop(lba, None)
        if old:
            self.state[old[0]][old[1]] = "I"
            self.owner[old[0]][old[1]] = None
            self.pagedata.pop(old, None)
            self.events.append(f"TRIM LBA {lba:02d}: página B{old[0]}:P{old[1]} invalidada")

    def wear_report(self, title="SSD"):
        e = self.erase
        top = max(max(e), 1)
        lines = [f"[DESGASTE DAS CÉLULAS - {title}]  Nivelamento de desgaste: "
                 f"{'LIGADO' if self.wl else 'DESLIGADO'}",
                 f"Blocos físicos: {self.nblocks} x {self.ppb} páginas "
                 f"(capacidade lógica {self.cap} blocos + sobra para GC)"]
        for b in range(self.nblocks):
            bar = "█" * round(e[b] / top * 30)
            st = "".join(self.state[b])
            lines.append(f"  B{b:02d} [{st}] {bar:<30} {e[b]} apagamento(s)")
        avg = sum(e) / len(e)
        wa = self.nand_writes / self.host_writes if self.host_writes else 0
        lines.append(f"Apagamentos -> mínimo: {min(e)} | máximo: {max(e)} | média: {avg:.1f} "
                     f"| diferença: {max(e) - min(e)}")
        lines.append(f"Escritas pedidas: {self.host_writes} | escritas reais na memória: "
                     f"{self.nand_writes} | amplificação de escrita: {wa:.2f}")
        lines.append("Legenda das páginas: V = válida, I = inválida (aguarda GC), F = livre")
        return lines


# ======================================================================
#  CAMADA RAID
# ======================================================================
class Storage:
    def __init__(self, n_blocks, bs, level="none", ndisks=1, device="hdd",
                 tracks=200, ppb=4, wl=True):
        if level not in RAID_LEVELS:
            raise FSError("RAID inválido. Use: none, 0, 1, 4 ou 5")
        if level == "none":
            ndisks = 1
        minimo = {"none": 1, "0": 2, "1": 2, "4": 3, "5": 3}[level]
        if ndisks < minimo:
            raise FSError(f"RAID {level} precisa de pelo menos {minimo} discos.")
        self.level, self.D, self.bs, self.n = level, ndisks, bs, n_blocks
        self.device, self.tracks, self.ppb, self.wl = device, tracks, ppb, wl
        self.data_disks = {"none": 1, "0": ndisks, "1": 1, "4": ndisks - 1, "5": ndisks - 1}[level]
        self.cap = n_blocks if level in ("none", "1") else math.ceil(n_blocks / self.data_disks)
        self.disks = [self._new_device() for _ in range(ndisks)]

    def _new_device(self):
        if self.device == "ssd":
            return SSD(self.cap, self.bs, self.ppb, self.wl)
        return HDD(self.cap, self.bs, self.tracks)

    def describe(self):
        nome = "Sem RAID (disco único)" if self.level == "none" else f"RAID {self.level}"
        return (f"Arranjo Ativo: {nome} ({self.D} Disco(s) Físico(s) {self.device.upper()}, "
                f"Blocos de {fmt_size(self.bs)})")

    # --- mapeamento
    def layout(self, stripe):
        if self.level == "4":
            parity = self.D - 1
        elif self.level == "5":
            parity = (self.D - stripe) % self.D   # paridade gira entre os discos
        else:
            parity = None
        return parity, [d for d in range(self.D) if d != parity]

    def locate(self, b):
        """Bloco do FS -> (disco, lba) onde fica o dado."""
        if self.level in ("none", "1"):
            return 0, b
        if self.level == "0":
            return b % self.D, b // self.D
        s, k = divmod(b, self.data_disks)
        _, data = self.layout(s)
        return data[k], s

    def stripe_blocks(self, s):
        return [s * self.data_disks + k for k in range(self.data_disks)
                if s * self.data_disks + k < self.n]

    def _alive(self, d):
        return not self.disks[d].failed

    def _rd(self, d, lba):
        return self.disks[d].read(lba)

    # --- escrita
    def write_block(self, b, data, desc):
        """Grava o bloco b. Devolve a lista de transações (disco, op, lba, descrição)."""
        ops, lvl = [], self.level
        if lvl in ("none", "0"):
            d, lba = self.locate(b)
            if not self._alive(d):
                raise FSError(f"Disco {d + 1} falhou e {'não há RAID' if lvl == 'none' else 'RAID 0 não tem redundância'}: "
                              "escrita impossível.")
            self.disks[d].write(lba, data)
            ops.append((d, "W", lba, f"Bloco FS {b:02d}: {desc}"))
        elif lvl == "1":
            alive = [d for d in range(self.D) if self._alive(d)]
            if not alive:
                raise FSError("Todos os discos do espelho falharam.")
            for d in alive:
                self.disks[d].write(b, data)
                ops.append((d, "W", b, f"Bloco FS {b:02d}: {desc}" + (" (espelho)" if d != alive[0] else "")))
        else:
            s, k = divmod(b, self.data_disks)
            parity, data_disks = self.layout(s)
            d = data_disks[k]
            failed = [x for x in range(self.D) if not self._alive(x)]
            if len(failed) > 1:
                raise FSError(f"RAID {lvl} com {len(failed)} discos em falha: dados perdidos.")
            members = ", ".join(f"Bloco {x:02d}" for x in self.stripe_blocks(s))
            pdesc = f"PARIDADE: XOR({members})"
            if not failed:
                # escrita "lê-modifica-escreve": nova paridade = paridade antiga XOR dado antigo XOR dado novo
                old, oldp = self._rd(d, s), self._rd(parity, s)
                newp = xor_blocks(oldp, old, data)
                ops += [(d, "R", s, f"lê dado antigo do Bloco FS {b:02d}"),
                        (parity, "R", s, "lê paridade antiga")]
                self.disks[d].write(s, data)
                self.disks[parity].write(s, newp)
                ops += [(d, "W", s, f"Bloco FS {b:02d}: {desc}"), (parity, "W", s, pdesc)]
            elif failed[0] == parity:
                self.disks[d].write(s, data)
                ops.append((d, "W", s, f"Bloco FS {b:02d}: {desc} (disco de paridade em falha)"))
            else:
                # modo degradado: recalcula paridade com os outros blocos da faixa
                others = [x for x in data_disks if x != d]
                parts = [data] + [self._rd(x, s) for x in others]
                ops += [(x, "R", s, "lê bloco vizinho (modo degradado)") for x in others]
                self.disks[parity].write(s, xor_blocks(*parts))
                ops.append((parity, "W", s, pdesc + f" (Bloco FS {b:02d} só existe via paridade - "
                                                    f"Disco {d + 1} em falha)"))
        return ops

    # --- leitura
    def read_block(self, b):
        """Devolve (dados, transações, observações)."""
        lvl, notes = self.level, []
        if lvl in ("none", "0"):
            d, lba = self.locate(b)
            if not self._alive(d):
                raise FSError(f"Bloco FS {b:02d} PERDIDO: Disco {d + 1} falhou e "
                              f"{'não há RAID' if lvl == 'none' else 'RAID 0 não tem redundância'}.")
            return self._rd(d, lba), [(d, "R", lba, f"Bloco FS {b:02d}")], notes
        if lvl == "1":
            alive = [d for d in range(self.D) if self._alive(d)]
            if not alive:
                raise FSError("Todos os discos do espelho falharam: dados perdidos.")
            if alive[0] != 0:
                notes.append(f"Disco 1 em falha: Bloco FS {b:02d} lido do espelho (Disco {alive[0] + 1}) [OK]")
            return self._rd(alive[0], b), [(alive[0], "R", b, f"Bloco FS {b:02d}")], notes
        s, k = divmod(b, self.data_disks)
        parity, data_disks = self.layout(s)
        d = data_disks[k]
        if self._alive(d):
            return self._rd(d, s), [(d, "R", s, f"Bloco FS {b:02d}")], notes
        others = [x for x in range(self.D) if x != d]
        if not all(self._alive(x) for x in others):
            raise FSError(f"Bloco FS {b:02d} PERDIDO: mais de um disco em falha no RAID {lvl}.")
        data = xor_blocks(*[self._rd(x, s) for x in others])
        ops = [(x, "R", s, f"lê para reconstruir Bloco FS {b:02d}") for x in others]
        notes.append(f"Recuperando Bloco FS {b:02d} via operação XOR: (" +
                     " XOR ".join(f"Disco {x + 1}" for x in others) + ") -> Reconstrução [OK]")
        return data, ops, notes

    def trim(self, b):
        if self.level in ("4", "5"):
            return  # o dado antigo continua valendo para a paridade da faixa
        if self.level == "1":
            for d in range(self.D):
                if self._alive(d):
                    self.disks[d].trim(b)
            return
        d, lba = self.locate(b)
        if self._alive(d):
            self.disks[d].trim(lba)

    # --- falha e reconstrução
    def fail(self, d):
        if not 0 <= d < self.D:
            raise FSError(f"Disco inválido. Use 1 a {self.D}.")
        self.disks[d].failed = True
        self.disks[d].data = {} if isinstance(self.disks[d], HDD) else None
        alive = self.D - sum(x.failed for x in self.disks)
        ok = {"none": alive == 1, "0": alive == self.D, "1": alive >= 1,
              "4": alive >= self.D - 1, "5": alive >= self.D - 1}[self.level]
        return ok

    def rebuild(self, d):
        if not 0 <= d < self.D:
            raise FSError(f"Disco inválido. Use 1 a {self.D}.")
        if not self.disks[d].failed:
            raise FSError(f"Disco {d + 1} não está em falha.")
        others = [x for x in range(self.D) if x != d]
        if self.level in ("none", "0") or not all(self._alive(x) for x in others) and self.level != "1":
            self.disks[d] = self._new_device()
            return None
        src = [x for x in others if self._alive(x)]
        if not src:
            return None
        new = self._new_device()
        count = 0
        for lba in range(self.cap):
            if self.level == "1":
                data = self._rd(src[0], lba)
            else:
                data = xor_blocks(*[self._rd(x, lba) for x in others])
            if any(data):
                new.write(lba, data)
                count += 1
        new.events = []
        self.disks[d] = new
        return count


# ======================================================================
#  SHELL DA ETAPA 2 (estende o shell da Etapa 1)
# ======================================================================
HELP_IO = """
COMANDOS DA ETAPA 2 (ENTRADA/SAÍDA)
  write_file <caminho> "texto" | --size N
                                   Grava arquivo (cria se não existir) e mostra a E/S gerada
  read_file <caminho>              Lê o arquivo do disco físico e confere os dados | alias: cat
  device hdd|ssd [--tracks 200] [--pages 4]
                                   Tipo de disco (HDD com braço ou SSD com páginas)
  algo fcfs|sstf|scan|cscan|look|clook
                                   Algoritmo de escalonamento do braço (HDD)
  head <trilha> [--dir up|down] [--disk N]
                                   Posição atual da cabeça de leitura
  queue add <t1> <t2> ... [--disk N] | queue show | queue clear
                                   Requisições pendentes de outros processos
  simulate --head 53 --queue 98,183,37,122,14 [--algo sstf|all] [--tracks 200] [--dir up]
                                   Simula o escalonamento sem mexer nos arquivos
  raid none|0|1|4|5 [--disks N]    Monta o arranjo RAID (os dados atuais são migrados)
  raid_status                      Mostra a distribuição dos blocos em cada disco
  fail <N>  | rebuild <N>          Simula falha / troca e reconstrução do disco N
  ssd_status | wl on|off           Desgaste do SSD / liga-desliga o nivelamento de desgaste
  wear_test <arquivo> --writes N   Regrava o arquivo N vezes e mostra o desgaste
  wear_compare [--writes 300]      Compara desgaste COM e SEM wear leveling
  io_status                        Resumo da camada de E/S
  demo2                            Demonstração completa da Etapa 2
"""

DEMO2 = [
    "# --- Exemplo 2.1: escalonamento SSTF (dados do enunciado) ---",
    "simulate --algo sstf --head 53 --queue 98,183,37,122,14",
    "simulate --algo all --head 53 --queue 98,183,37,122,14",
    "# --- Integração FS -> E/S em HDD ---",
    "format --block 4KB --blocks 16 --strategy indexada",
    "device hdd --tracks 200",
    "algo sstf",
    "mkdir -p /sistema/logs",
    "create /sistema/logs/app.log --size 10KB",
    "head 53",
    "queue add 98 183 37 122 14",
    "write_file /sistema/logs/app.log \"Novo Registro\"",
    "read_file /sistema/logs/app.log",
    "# --- Exemplo 2.2: RAID 5 com falha de disco ---",
    "format --block 4KB --blocks 16 --strategy indexada",
    "raid 5 --disks 3",
    "mkdir /dados",
    "write_file /dados/planilha.csv --size 8KB",
    "raid_status",
    "fail 2",
    "read_file /dados/planilha.csv",
    "rebuild 2",
    "raid_status",
    "# --- SSD: páginas, escrita fora do lugar e wear leveling ---",
    "raid none",
    "device ssd --pages 4",
    "write_file /dados/planilha.csv \"versao 2\"",
    "wear_compare --writes 300",
]


class IOShell(Shell):
    def __init__(self):
        super().__init__()
        self.cfg = {"device": "hdd", "tracks": 200, "algo": "sstf", "raid": "none",
                    "disks": 1, "ppb": 4, "wl": True}
        self.st = None
        self.build_storage(migrate=False)

    # ------------------------------------------------ dados dos arquivos
    @staticmethod
    def file_bytes(f):
        if f.content is not None:
            return f.content.encode("utf-8")
        if not hasattr(f, "_seed"):
            f._seed = f.path()
        pattern = (f._seed + "|").encode("utf-8")
        return (pattern * (f.size // len(pattern) + 1))[: f.size]

    def chunks(self, f):
        bs = self.fs.disk.bs
        raw = self.file_bytes(f)
        return [raw[i * bs:(i + 1) * bs].ljust(bs, b"\0") for i in range(len(f.blocks))]

    def inode_bytes(self, f):
        txt = f"INODE {f.name} size={f.size} ptr={f.blocks}".encode("utf-8")
        return txt[: self.fs.disk.bs].ljust(self.fs.disk.bs, b"\0")

    # ------------------------------------------------ armazenamento
    def build_storage(self, migrate=True):
        c = self.cfg
        st = Storage(self.fs.disk.n, self.fs.disk.bs, c["raid"], c["disks"], c["device"],
                     c["tracks"], c["ppb"], c["wl"])
        self.st = st
        if migrate:
            for f in self.fs.all_files():
                for b, chunk in zip(f.blocks, self.chunks(f)):
                    st.write_block(b, chunk, "")
                if f.inode is not None:
                    st.write_block(f.inode, self.inode_bytes(f), "")
            for d in st.disks:
                d.events = []

    def write_file_io(self, f):
        ops = []
        for seq, (b, chunk) in enumerate(zip(f.blocks, self.chunks(f))):
            used = min(self.fs.disk.bs, f.size - seq * self.fs.disk.bs)
            ops += self.st.write_block(b, chunk, f"{f.name} - Parte {seq + 1} ({fmt_size(used)})")
        if f.inode is not None:
            ops += self.st.write_block(f.inode, self.inode_bytes(f), f"i-node de {f.name}")
        return ops

    # ------------------------------------------------ despacho das transações
    def dispatch(self, ops, notes=()):
        st, out = self.st, []
        if not ops:
            return ""
        nr = sum(1 for o in ops if o[1] == "R")
        out.append(f"\n[E/S] {len(ops)} transação(ões) gerada(s) pelo Sistema de Arquivos "
                   f"(leituras: {nr}, gravações: {len(ops) - nr})")
        out.append(st.describe())
        for d, op, lba, desc in ops:
            dev = st.disks[d]
            pos = f"trilha {dev.track_of(lba):3d}" if dev.kind == "HDD" else "página via FTL"
            out.append(f"  {'LEITURA' if op == 'R' else 'GRAVAÇÃO':<8} Disco {d + 1} | LBA {lba:02d} | "
                       f"{pos} | {desc}")

        if st.level != "none" and any(o[1] == "W" for o in ops):
            out.append("\n[E/S - DISTRIBUIÇÃO EM DISCO]")
            for d in range(st.D):
                ws = list({o[2]: o for o in ops if o[0] == d and o[1] == "W"}.values())
                if st.disks[d].failed:
                    out.append(f"Disco {d + 1}: [EM FALHA - nada gravado]")
                for _, _, lba, desc in ws:
                    out.append(f"Disco {d + 1}: [Faixa {lba:02d}: {desc}]")
        for n in notes:
            out.append(f"* {n}")

        for d in range(st.D):
            dev = st.disks[d]
            mine = [o for o in ops if o[0] == d]
            if dev.kind == "HDD":
                if not mine and not dev.pending:
                    continue
                tracks = list(dev.pending) + [dev.track_of(o[2]) for o in mine]
                tag = f" (Disco {d + 1})" if st.D > 1 else ""
                out.append("")
                if dev.pending:
                    out.append(f"[E/S]{tag} Requisições de outros processos já na fila: {dev.pending}")
                out.append(f"[E/S]{tag} Requisições de leitura/escrita pendentes nas trilhas: {tracks}")
                out.append(f"Posição atual da cabeça de leitura: Trilha {dev.head}")
                lines, steps, total = schedule_report(dev.head, tracks, self.cfg["algo"],
                                                      dev.tracks - 1, dev.direction)
                out += lines
                path = [dev.head] + [t for t, _ in steps]
                if self.cfg["algo"] in ("scan", "look") and len(path) > 1 and path[-1] != path[-2]:
                    dev.direction = "up" if path[-1] > path[-2] else "down"
                if steps:
                    dev.head = steps[-1][0]
                dev.total_moves += total
                dev.pending = []
            else:
                if dev.events:
                    tag = f" (Disco {d + 1})" if st.D > 1 else ""
                    out.append(f"\n[E/S - SSD{tag}] Sem braço mecânico: não há busca de trilha, "
                               "requisições atendidas na ordem de chegada")
                    out += [f"  {e}" for e in dev.events]
                    dev.events = []
        return "\n".join(out)

    def trim_freed(self, used_before):
        used_now = {i for i, b in enumerate(self.fs.disk.blocks) if b is not None}
        for b in sorted(used_before - used_now):
            self.st.trim(b)

    def used_blocks(self):
        return {i for i, b in enumerate(self.fs.disk.blocks) if b is not None}

    def ssd_events_text(self):
        out = []
        for i, d in enumerate(self.st.disks):
            if d.kind == "SSD" and d.events:
                out.append(f"[E/S - SSD Disco {i + 1}]")
                out += [f"  {e}" for e in d.events]
                d.events = []
        return "\n".join(out)

    # ------------------------------------------------ comandos
    def run(self, line):
        if line.strip().startswith("#"):
            return line.strip()
        tokens = shlex.split(line)
        if not tokens:
            return ""
        cmd, args = tokens[0].lower(), tokens[1:]

        handler = getattr(self, f"cmd_{cmd}", None)
        if handler:
            return handler(args)

        if cmd == "help":
            return HELP_FS + HELP_IO
        if cmd == "format":
            out = super().run(line)
            self.build_storage(migrate=False)
            return out + "\n" + self.st.describe()
        if cmd in ("create", "touch", "write", "update", "append"):
            p, _ = parse_args(args)
            before = self.used_blocks()
            out = super().run(line)
            f = self.fs.resolve(p[0])
            self.trim_freed(before)
            return out + self.dispatch(self.write_file_io(f))
        if cmd in ("cat", "read"):
            return self.cmd_read_file(args)
        if cmd in ("rm", "rmdir"):
            before = self.used_blocks()
            out = super().run(line)
            self.trim_freed(before)
            freed = sorted(before - self.used_blocks())
            msg = f"[E/S] Blocos liberados: {[f'{b:02d}' for b in freed]}"
            if self.cfg["device"] == "ssd" and self.st.level not in ("4", "5"):
                msg += " -> comando TRIM enviado ao SSD"
            ev = self.ssd_events_text()
            return "\n".join(x for x in (out, msg, ev) if x)
        if cmd in ("defrag", "compact"):
            return self.defrag_io(line)
        if cmd == "demo2":
            for c in DEMO2:
                print(f"\n> {c}")
                self.safe_run(c)
            return "\n[Fim da demonstração da Etapa 2]"
        return super().run(line)

    def defrag_io(self, line):
        before = {f: (list(f.blocks), f.inode) for f in self.fs.all_files()}
        used_before = self.used_blocks()
        out = super().run(line)
        moves = []
        for f, (old_blocks, old_inode) in before.items():
            moves += [(o, n, f"{f.name} - Parte {i + 1}") for i, (o, n) in
                      enumerate(zip(old_blocks, f.blocks)) if o != n]
            if old_inode is not None and old_inode != f.inode:
                moves.append((old_inode, f.inode, f"i-node de {f.name}"))
        ops = []
        for old, new, desc in sorted(moves):   # de trás para frente não sobrescreve nada
            data, r, _ = self.st.read_block(old)
            ops += r
            ops += self.st.write_block(new, data, f"{desc} (movido do bloco {old:02d})")
        for f in before:  # i-nodes guardam os novos ponteiros
            if f.inode is not None and any(m[2].startswith(f.name) for m in moves):
                ops += self.st.write_block(f.inode, self.inode_bytes(f), f"i-node de {f.name} atualizado")
        self.trim_freed(used_before)
        return out + self.dispatch(ops)

    def cmd_write_file(self, args):
        p, o = parse_args(args)
        if not p:
            raise FSError('Uso: write_file <caminho> "texto" | --size N')
        path = p[0]
        content = p[1] if len(p) > 1 else o.get("content")
        if content is None and "size" not in o:
            raise FSError('Informe o texto ou --size N.')
        node = self.fs.resolve(path)
        if isinstance(node, Directory):
            raise FSError(f"'{path}' é um diretório.")
        if node is None:
            parent = path.rstrip("/").rsplit("/", 1)[0] if "/" in path.strip("/") else ""
            if parent and self.fs.resolve(parent) is None:
                self.fs.mkdir(parent, parents=True)
            cmd = "create"
        else:
            cmd = "write"
        opt = f'--content {shlex.quote(content)}' if content is not None else f'--size {o["size"]}'
        return self.run(f"{cmd} {shlex.quote(path)} {opt}")

    def cmd_read_file(self, args):
        if not args:
            raise FSError("Uso: read_file <caminho>")
        f = self.fs.must_resolve(args[0])
        if not isinstance(f, File):
            raise FSError(f"'{args[0]}' é um diretório (use ls).")
        ops, notes, parts = [], [], []
        if f.inode is not None:
            _, o, n = self.st.read_block(f.inode)
            ops += o
            notes += n
        for b in f.blocks:
            data, o, n = self.st.read_block(b)
            parts.append(data)
            ops += o
            notes += n
        got = b"".join(parts)[: f.size]
        ok = got == self.file_bytes(f)
        out = []
        if self.fs.strategy == "encadeada" and f.blocks:
            out.append("Seguindo a FAT: " + " -> ".join(f"{b:02d}" for b in f.blocks) + " -> FIM")
        if f.content is not None:
            out.append(f"Conteúdo: {got.decode('utf-8', errors='replace')}")
        else:
            out.append(f"[conteúdo simulado de {f.size} bytes lido do disco]")
        out.append(f"[VERIFICAÇÃO] dados lidos do disco físico == dados gravados: "
                   f"{'OK' if ok else 'FALHA - DADOS CORROMPIDOS'}")
        return "\n".join(out) + self.dispatch(ops, notes)

    def cmd_device(self, args):
        p, o = parse_args(args)
        if not p or p[0].lower() not in ("hdd", "ssd"):
            raise FSError("Uso: device hdd|ssd [--tracks 200] [--pages 4]")
        self.cfg["device"] = p[0].lower()
        if "tracks" in o:
            t = int(o["tracks"])
            if t < 2:
                raise FSError("O disco precisa de pelo menos 2 trilhas.")
            self.cfg["tracks"] = t
        if "pages" in o:
            pg = int(o["pages"])
            if pg < 2:
                raise FSError("Use pelo menos 2 páginas por bloco.")
            self.cfg["ppb"] = pg
        self.build_storage()
        extra = (f"{self.cfg['tracks']} trilhas (0 a {self.cfg['tracks'] - 1})"
                 if self.cfg["device"] == "hdd"
                 else f"{self.cfg['ppb']} páginas por bloco de apagamento")
        return f"Dispositivo: {self.cfg['device'].upper()} | {extra} (dados atuais migrados)\n" + self.st.describe()

    def cmd_algo(self, args):
        if not args or args[0].lower() not in ALGOS:
            raise FSError(f"Uso: algo {'|'.join(ALGOS)}")
        self.cfg["algo"] = args[0].lower()
        return f"Escalonamento do braço: {ALGOS[self.cfg['algo']]}"

    def _disk_arg(self, o):
        d = int(o.get("disk", 1)) - 1
        if not 0 <= d < self.st.D:
            raise FSError(f"Disco inválido. Use 1 a {self.st.D}.")
        if self.st.disks[d].kind != "HDD":
            raise FSError("Este comando vale só para HDD (SSD não tem cabeça de leitura).")
        return self.st.disks[d]

    def cmd_head(self, args):
        p, o = parse_args(args)
        dev = self._disk_arg(o)
        if p:
            t = int(p[0])
            if not 0 <= t < dev.tracks:
                raise FSError(f"Trilha deve estar entre 0 e {dev.tracks - 1}.")
            dev.head = t
        if "dir" in o:
            if o["dir"] not in ("up", "down"):
                raise FSError("Use --dir up ou --dir down.")
            dev.direction = o["dir"]
        return f"Cabeça de leitura na trilha {dev.head} (direção: {dev.direction})"

    def cmd_queue(self, args):
        p, o = parse_args(args)
        if not p:
            raise FSError("Uso: queue add <trilhas...> | queue show | queue clear")
        if p[0] == "show":
            return "\n".join(f"Disco {i + 1}: {d.pending}" for i, d in enumerate(self.st.disks)
                             if d.kind == "HDD")
        dev = self._disk_arg(o)
        if p[0] == "clear":
            dev.pending = []
            return "Fila limpa."
        if p[0] == "add":
            for t in p[1:]:
                for x in t.split(","):
                    if x:
                        v = int(x)
                        if not 0 <= v < dev.tracks:
                            raise FSError(f"Trilha {v} fora do disco (0 a {dev.tracks - 1}).")
                        dev.pending.append(v)
            return f"Fila de requisições de outros processos: {dev.pending}"
        raise FSError("Uso: queue add <trilhas...> | queue show | queue clear")

    def cmd_simulate(self, args):
        _, o = parse_args(args)
        head = int(o.get("head", 53))
        tracks = int(o.get("tracks", self.cfg["tracks"]))
        direction = o.get("dir", "up")
        try:
            q = [int(x) for x in o.get("queue", "98,183,37,122,14").replace(" ", "").split(",") if x]
        except ValueError:
            raise FSError("Fila inválida. Ex.: --queue 98,183,37,122,14")
        if not q:
            raise FSError("A fila está vazia.")
        if any(not 0 <= t < tracks for t in q + [head]):
            raise FSError(f"Trilhas devem estar entre 0 e {tracks - 1}.")
        algo = o.get("algo", self.cfg["algo"]).lower()
        if algo != "all" and algo not in ALGOS:
            raise FSError(f"Algoritmo inválido. Use: {', '.join(ALGOS)} ou all")
        head_txt = [f"[E/S] Requisições de leitura/escrita pendentes nas trilhas: {q}",
                    f"Posição atual da cabeça de leitura: Trilha {head} (disco de 0 a {tracks - 1})"]
        if algo != "all":
            lines, _, _ = schedule_report(head, q, algo, tracks - 1, direction)
            return "\n".join(head_txt + [""] + lines)
        out = head_txt + ["", "--- COMPARATIVO DE ALGORITMOS ---"]
        res = []
        for a in ALGOS:
            steps = schedule(head, q, a, tracks - 1, direction)
            path = [head] + [t for t, _ in steps]
            total = sum(abs(x - y) for x, y in zip(path, path[1:]))
            res.append((total, a, path))
        best = min(r[0] for r in res)
        for total, a, path in res:
            mark = "  <- menor deslocamento" if total == best else ""
            out.append(f"{ALGOS[a]:<34}: {total:4d} trilhas | {' -> '.join(map(str, path))}{mark}")
        return "\n".join(out)

    def cmd_raid(self, args):
        p, o = parse_args(args)
        if not p or p[0].lower() not in RAID_LEVELS:
            raise FSError("Uso: raid none|0|1|4|5 [--disks N]")
        lvl = p[0].lower()
        default = {"none": 1, "0": 2, "1": 2, "4": 3, "5": 3}[lvl]
        disks = int(o.get("disks", default))
        old = dict(self.cfg)
        self.cfg["raid"], self.cfg["disks"] = lvl, disks
        try:
            self.build_storage()
        except FSError:
            self.cfg = old
            raise
        info = {"none": "disco único, sem redundância",
                "0": "distribuição (striping) sem redundância - mais rápido, uma falha perde dados",
                "1": "espelhamento - cada bloco gravado em todos os discos",
                "4": "striping + disco de paridade dedicado (último disco)",
                "5": "striping + paridade distribuída (gira entre os discos)"}[lvl]
        return (f"{self.st.describe()}\nTipo: {info}\nCapacidade de cada disco: {self.st.cap} blocos "
                "(dados atuais migrados para o novo arranjo)")

    def cmd_raid_status(self, args):
        st = self.st
        out = [st.describe(), ""]
        head = "Faixa |" + "|".join(f"{'Disco ' + str(d + 1) + (' (FALHA)' if st.disks[d].failed else ''):^18}"
                                    for d in range(st.D))
        out += [head, "-" * len(head)]
        used = self.used_blocks()
        for lba in range(st.cap):
            cells = []
            for d in range(st.D):
                if st.disks[d].failed:
                    cells.append("XXXX")
                    continue
                if st.level in ("none", "1"):
                    b = lba
                    cells.append(f"Bloco {b:02d}" if b in used else "livre")
                elif st.level == "0":
                    b = lba * st.D + d
                    cells.append(f"Bloco {b:02d}" if b < st.n and b in used else "livre")
                else:
                    parity, data = st.layout(lba)
                    if d == parity:
                        cells.append("P = XOR(" + ",".join(f"{x:02d}" for x in st.stripe_blocks(lba)) + ")")
                    else:
                        b = lba * st.data_disks + data.index(d)
                        cells.append(f"Bloco {b:02d}" if b < st.n and b in used else "livre")
            if any(c.startswith("Bloco") for c in cells) or any(st.disks[d].failed for d in range(st.D)):
                out.append(f"{lba:^6}|" + "|".join(f"{c:^18}" for c in cells))
        out.append("(só aparecem as faixas que guardam algum bloco em uso)")
        return "\n".join(out)

    def cmd_fail(self, args):
        if not args:
            raise FSError("Uso: fail <N>")
        d = int(args[0]) - 1
        ok = self.st.fail(d)
        msg = [f"[TESTE DE FALHA SIMULADO]", f"* Falha forçada no Disco {d + 1}! (conteúdo do disco perdido)"]
        msg.append("* O arranjo continua funcionando em modo degradado." if ok else
                   "* ATENÇÃO: o arranjo não tem redundância suficiente - há dados PERDIDOS.")
        return "\n".join(msg)

    def cmd_rebuild(self, args):
        if not args:
            raise FSError("Uso: rebuild <N>")
        d = int(args[0]) - 1
        n = self.st.rebuild(d)
        if n is None:
            return (f"Disco {d + 1} substituído por um disco novo, mas NÃO foi possível reconstruir "
                    "os dados (sem redundância). Os blocos que estavam nele foram perdidos.")
        how = "copiando do espelho" if self.st.level == "1" else "XOR dos discos restantes"
        verify = []
        for f in self.fs.all_files():
            try:
                data = b"".join(self.st.read_block(b)[0] for b in f.blocks)[: f.size]
                verify.append(f"  {f.path()}: {'OK' if data == self.file_bytes(f) else 'FALHA'}")
            except FSError as e:
                verify.append(f"  {f.path()}: {e}")
        return "\n".join([f"Disco {d + 1} trocado e reconstruído ({how}): {n} bloco(s) recriado(s) [OK]",
                          "Verificação dos arquivos:"] + verify)

    def cmd_ssd_status(self, args):
        out = []
        for i, d in enumerate(self.st.disks):
            if d.kind != "SSD":
                continue
            if d.failed:
                out.append(f"Disco {i + 1}: EM FALHA")
                continue
            out += d.wear_report(f"Disco {i + 1}")
            m = ", ".join(f"{l:02d}->B{b}:P{p}" for l, (b, p) in sorted(d.map.items()))
            out.append(f"Mapa FTL (LBA -> página física): {m or '(vazio)'}")
        return "\n".join(out) or "Nenhum SSD ativo (use: device ssd)."

    def cmd_wl(self, args):
        if not args or args[0].lower() not in ("on", "off"):
            raise FSError("Uso: wl on|off")
        self.cfg["wl"] = args[0].lower() == "on"
        for d in self.st.disks:
            if d.kind == "SSD":
                d.wl = self.cfg["wl"]
        return f"Wear leveling {'LIGADO' if self.cfg['wl'] else 'DESLIGADO'}."

    def cmd_wear_test(self, args):
        p, o = parse_args(args)
        if not p:
            raise FSError("Uso: wear_test <arquivo> --writes N")
        if self.cfg["device"] != "ssd":
            raise FSError("Ative o SSD primeiro: device ssd")
        n = int(o.get("writes", 100))
        if not 1 <= n <= 100000:
            raise FSError("Use --writes entre 1 e 100000.")
        f = self.fs.must_resolve(p[0])
        if not isinstance(f, File):
            raise FSError("Informe um arquivo.")
        for _ in range(n):
            before = self.used_blocks()
            self.fs.write(p[0], size=None if f.content is not None else f.size,
                          content=f.content)
            self.trim_freed(before)
            self.write_file_io(f)
        events = sum(len(d.events) for d in self.st.disks if d.kind == "SSD")
        for d in self.st.disks:
            if d.kind == "SSD":
                d.events = []
        return (f"Arquivo '{f.path()}' regravado {n} vez(es) ({events} eventos de E/S no SSD).\n"
                + self.cmd_ssd_status([]))

    def cmd_wear_compare(self, args):
        _, o = parse_args(args)
        n = int(o.get("writes", 300))
        if not 1 <= n <= 100000:
            raise FSError("Use --writes entre 1 e 100000.")
        out = [f"Cenário: SSD de 16 blocos lógicos. LBAs 00-11 = dados FRIOS (gravados 1 vez).",
               f"LBAs 12-15 = dados QUENTES, regravados {n} vezes no total.", ""]
        for wl in (False, True):
            s = SSD(16, 16, self.cfg["ppb"], wl)
            for lba in range(12):
                s.write(lba, bytes([lba]) * 16)
            for i in range(n):
                s.write(12 + i % 4, bytes([i % 256]) * 16)
            out += s.wear_report("SEM wear leveling" if not wl else "COM wear leveling") + [""]
        out.append("Conclusão: sem nivelamento, poucos blocos concentram os apagamentos e "
                   "desgastam primeiro;\ncom nivelamento, o desgaste fica distribuído "
                   "(diferença entre máximo e mínimo menor).")
        return "\n".join(out)

    def cmd_io_status(self, args):
        st = self.st
        out = [st.describe(), f"Escalonamento (HDD): {ALGOS[self.cfg['algo']]}"]
        for i, d in enumerate(st.disks):
            if d.kind == "HDD":
                out.append(f"Disco {i + 1} [HDD] {'FALHA' if d.failed else 'OK'} | cabeça na trilha "
                           f"{d.head} ({d.direction}) | fila: {d.pending} | movimento acumulado: "
                           f"{d.total_moves} trilhas")
            else:
                out.append(f"Disco {i + 1} [SSD] {'FALHA' if d.failed else 'OK'} | apagamentos "
                           f"mín/máx: {min(d.erase)}/{max(d.erase)} | wear leveling: "
                           f"{'ligado' if d.wl else 'desligado'}")
        return "\n".join(out)


def main():
    sh = IOShell()
    interactive = sys.stdin.isatty()
    print("Simulador de Sistema de Arquivos + Entrada/Saída - Etapa 2  "
          "(digite 'help', 'demo' ou 'demo2')")
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
