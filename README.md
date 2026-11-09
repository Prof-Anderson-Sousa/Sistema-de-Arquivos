# Simulador de Sistema de Arquivos + Entrada/Saída

Simulador em **Python 3** (sem bibliotecas externas), com linha de comando interativa.

| Arquivo | Conteúdo |
|---|---|
| `fs_sim.py` | **Etapa 1** – Sistema de Arquivos (árvore, CRUD, alocação contígua / encadeada / indexada, fragmentação) |
| `io_sim.py` | **Etapa 2** – Entrada/Saída (transações de E/S, escalonamento do braço, SSD com wear leveling, RAID) |
| `roteiro_testes_etapa2.txt` | Roteiro de testes da Etapa 2 |

## Como executar

```bash
python io_sim.py                              # modo interativo (Etapa 1 + Etapa 2)
python io_sim.py < roteiro_testes_etapa2.txt  # roda o roteiro de testes
```

Dentro do simulador: `help`, `demo` (Etapa 1) ou `demo2` (Etapa 2).
Todos os comandos da Etapa 1 continuam valendo no `io_sim.py`.

---

## Etapa 2 – Entrada/Saída

### Como as camadas se conectam

```
Comando do usuário (create, write_file, read_file, rm, defrag...)
        │
Sistema de Arquivos (Etapa 1)  -> decide quais blocos o arquivo usa
        │   cada bloco lido/gravado vira uma TRANSAÇÃO DE E/S
Camada RAID (none, 0, 1, 4, 5) -> decide em qual disco/faixa o bloco fica + paridade XOR
        │
Dispositivo físico
   HDD -> fila de requisições + escalonamento do braço (trilhas)
   SSD -> FTL: bloco lógico -> página física, GC e wear leveling
```

### 1. Integração com o Sistema de Arquivos
- `create`, `write`, `append`, `write_file` → gravação dos blocos de dados **e** do i-node.
- `read_file` / `cat` → leitura dos blocos no disco físico e **verificação** de que os dados lidos são iguais aos gravados.
- `rm` → libera os blocos (no SSD envia **TRIM**).
- `defrag` → gera leituras e gravações para mover os blocos.

### 2. Escalonamento do braço (HDD) e SSD
- Algoritmos: **FCFS, SSTF, SCAN, C-SCAN, LOOK, C-LOOK** (`algo ...`).
- Cada bloco é mapeado para uma trilha do disco (padrão: 200 trilhas, de 0 a 199).
- A fila junta as requisições do sistema de arquivos com as de outros processos (`queue add`).
- Mostra a ordem de atendimento, o cálculo do deslocamento e o total de trilhas.
- A posição da cabeça é mantida entre um comando e outro.
- `simulate --algo all` compara todos os algoritmos.
- **SSD** (`device ssd`): não tem braço. Usa páginas e blocos de apagamento, grava sempre em uma página nova (a antiga fica inválida), faz coleta de lixo e **wear leveling** dinâmico e estático. `wear_compare` mostra o desgaste com e sem nivelamento.

### 3. RAID
- **RAID 0**: distribui os blocos entre os discos (sem redundância).
- **RAID 1**: espelhamento.
- **RAID 4**: paridade em disco fixo.
- **RAID 5**: paridade distribuída entre os discos.
- A paridade é um **XOR real** dos bytes. A gravação usa lê-modifica-escreve: nova paridade = paridade antiga XOR dado antigo XOR dado novo.
- `fail N` apaga o conteúdo do disco N. A leitura é reconstruída por XOR (RAID 4/5) ou pelo espelho (RAID 1). No RAID 0 os dados são perdidos.
- `rebuild N` troca o disco e reconstrói os dados.

### Comandos da Etapa 2

```
write_file <caminho> "texto" | --size N
read_file <caminho>
device hdd|ssd [--tracks 200] [--pages 4]
algo fcfs|sstf|scan|cscan|look|clook
head <trilha> [--dir up|down] [--disk N]
queue add <t1> <t2> ... [--disk N] | queue show | queue clear
simulate --head 53 --queue 98,183,37,122,14 [--algo sstf|all] [--tracks 200] [--dir up]
raid none|0|1|4|5 [--disks N]
raid_status | fail <N> | rebuild <N>
ssd_status | wl on|off | wear_test <arquivo> --writes N | wear_compare [--writes 300]
io_status | demo2
```

### Exemplo 2.1 — SSTF

```
> simulate --algo sstf --head 53 --queue 98,183,37,122,14
[E/S] Requisições de leitura/escrita pendentes nas trilhas: [98, 183, 37, 122, 14]
Posição atual da cabeça de leitura: Trilha 53 (disco de 0 a 199)

--- ESCALONAMENTO DE DISCO: SSTF (Shortest Seek Time First) ---
Ordem de Atendimento  : 53 -> 37 -> 14 -> 98 -> 122 -> 183
Deslocamento da Cabeça: |53-37| + |37-14| + |14-98| + |98-122| + |122-183|
Total de Movimento    : 16 + 23 + 84 + 24 + 61 = 208 trilhas
```

### Exemplo 2.2 — RAID 5 com falha

```
> raid 5 --disks 3
> write_file /dados/planilha.csv --size 8KB
[E/S - DISTRIBUIÇÃO EM DISCO]
Disco 1: [Faixa 01: Bloco FS 02: planilha.csv - Parte 1 (4 KB)]
Disco 2: [Faixa 01: Bloco FS 03: planilha.csv - Parte 2 (4 KB)]
Disco 3: [Faixa 01: PARIDADE: XOR(Bloco 02, Bloco 03)]
...
> fail 2
[TESTE DE FALHA SIMULADO]
* Falha forçada no Disco 2! (conteúdo do disco perdido)
> read_file /dados/planilha.csv
[VERIFICAÇÃO] dados lidos do disco físico == dados gravados: OK
* Recuperando Bloco FS 03 via operação XOR: (Disco 1 XOR Disco 3) -> Reconstrução [OK]
```

### Trechos de código mais relevantes (`io_sim.py`)
- `schedule()` / `schedule_report()` — algoritmos de escalonamento e cálculo do deslocamento.
- `class SSD` — FTL, escrita fora do lugar, coleta de lixo (`_gc`) e wear leveling (`_take_block`, `_static_wear_leveling`).
- `class Storage` — RAID: `write_block` (paridade lê-modifica-escreve), `read_block` (reconstrução XOR), `rebuild`.
- `IOShell.dispatch()` — transforma as transações do sistema de arquivos em filas por disco e mostra o atendimento.

### Observações
- Tudo roda em memória (não há persistência, que é opcional).
- Na alocação indexada, o bloco 00 é o i-node da raiz `/`. Ele nunca é gravado com dados, por isso aparece vazio no RAID.
