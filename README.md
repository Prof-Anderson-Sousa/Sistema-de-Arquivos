# Simulador de Sistema de Arquivos (FS) — Etapa 1

Simulador em **Python 3** (sem bibliotecas externas) com interface de linha de comando interativa.

## O que foi implementado

| Requisito | Como está no simulador |
|---|---|
| Hierarquia em árvore | Diretórios e arquivos em árvore, caminhos absolutos (`/home/user/doc.txt`) e relativos (`doc.txt`, `../logs`, `.`) |
| CRUD | **C**: `mkdir`, `create` · **R**: `cat`, `ls`, `tree`, `stat` · **U**: `write`, `append`, `mv` · **D**: `rm`, `rmdir` |
| Blocos de tamanho fixo | Disco simulado com tamanho de bloco e quantidade configuráveis (`format --block 4KB --blocks 16`) |
| Estratégias de alocação | As 3: `contigua`, `encadeada` (FAT) e `indexada` (i-node) |
| Diagnóstico do disco | `fs_status --details`: mapa gráfico, blocos por arquivo, fragmentação interna, fragmentação externa e necessidade de compactação |
| Compactação | `defrag` junta todos os blocos livres no fim do disco |

## Como executar

```bash
python fs_sim.py              # modo interativo
python fs_sim.py < roteiro.txt  # executa uma lista de comandos de um arquivo
```

Dentro do simulador, digite `help` para ver os comandos ou `demo` para rodar a demonstração completa.

## Comandos

```
format [--block 4KB] [--blocks 16] [--strategy indexada|contigua|encadeada]
mkdir [-p] <caminho>
create <caminho> [--size 10KB] [--content "texto"]
cat <caminho>
write <caminho> --size N | --content "texto"
append <caminho> --content "texto"
mv <origem> <destino>
rm [-r] <caminho>
rmdir [-r] <caminho>
ls [caminho] | cd <caminho> | pwd | tree [caminho] | stat <caminho>
fs_status [--details]
defrag
demo | help | exit
```

## Exemplo (alocação indexada)

```
> format --block 4KB --blocks 16 --strategy indexada
> mkdir /sistema
> mkdir /sistema/logs
> create /sistema/logs/app.log --size 10KB
> tree
[ÁRVORE DO SISTEMA DE ARQUIVOS]
/
└── sistema/ [DIR]
    └── logs/ [DIR]
        └── app.log (10 KB) [ARQ]

> fs_status --details
Tamanho do Bloco de Disco: 4 KB | Total de Blocos: 16 (64 KB) | Estratégia: Indexada (i-node)
...
  [R ] [iA] [A1] [A2] [A3] [..] [..] [..]
...
Bloco 00      [INODE_RAIZ]  : Diretório '/'
Bloco 01      [INODE_A]     : Arquivo '/sistema/logs/app.log' (ponteiros: 02,03,04)
Bloco 02      [DADO]        : '/sistema/logs/app.log' (Bloco 1 - 4096 bytes ocupados)
Bloco 03      [DADO]        : '/sistema/logs/app.log' (Bloco 2 - 4096 bytes ocupados)
Bloco 04      [DADO]        : '/sistema/logs/app.log' (Bloco 3 - 2048 bytes ocupados)
Bloco 05..15                  : [LIVRE]

--- RELATÓRIO DE FRAGMENTAÇÃO ---
Tamanho Real do Arquivo 'app.log': 10240 bytes (10 KB)
Espaço Alocado no Disco          : 12288 bytes (3 blocos de 4 KB)
Fragmentação Interna             : 2048 bytes (50% de desperdício no Bloco 04)
Fragmentação Externa             : 0 bytes
Necessidade de Compactação       : NÃO (blocos alocados via i-node indexado; ...)
```

## Exemplo (alocação contígua com fragmentação externa)

```
> format --block 1KB --blocks 12 --strategy contigua
> mkdir -p /home/user
> create /home/user/a.txt --size 3KB
> create /home/user/b.txt --size 2500B
> create /home/user/c.txt --size 3KB
> create /home/user/d.txt --size 2KB
> rm /home/user/b.txt
> create /home/user/e.txt --size 4KB
ERRO: FRAGMENTAÇÃO EXTERNA: o arquivo precisa de 4 blocos seguidos. Existem 4 blocos
livres, mas espalhados (maior lacuna = 3 blocos). Use 'defrag' para compactar o disco.
> defrag
> create /home/user/e.txt --size 4KB      (agora funciona)
```

## Trechos de código mais relevantes

- `FileSystem._allocate()` — as 3 estratégias de alocação (contígua com *first-fit*, encadeada com tabela FAT, indexada com bloco de i-node).
- `FileSystem.status()` — mapa gráfico do disco e relatório de fragmentação.
- `FileSystem.defrag()` — compactação, reorganizando blocos, ponteiros da FAT e i-nodes.
- `FileSystem.resolve()` — navegação por caminhos absolutos e relativos.

## Observações

- Tudo roda em memória (não há persistência, que é opcional na atividade).
- Na alocação indexada, a raiz `/` ocupa o bloco 0 (i-node raiz) e cada arquivo ocupa 1 bloco de i-node + os blocos de dados, como no exemplo do enunciado.
