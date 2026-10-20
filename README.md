# sysmon

Terminal system monitor. Requires Python 3 and psutil.

```
pip install psutil
python3 sysmon.py
```

## What it shows

- Per-core CPU bars with sparkline history
- Memory and swap usage
- Network TX/RX rates
- Process table with PID, CPU%, memory (% and GB), threads, status, user

## Keys

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate process list |
| `t` | Jump to top |
| `c` / `m` / `p` / `n` | Sort by CPU / memory / PID / name |
| `/` | Filter by name or PID — Enter to lock, ESC to clear |
| `k` | SIGTERM selected process (confirm prompt) |
| `K` | SIGKILL selected process (confirm prompt) |
| `H` | Hog sweep — flags zombies, CPU/mem hogs, duplicate groups for bulk kill |
| `q` | Quit |

## Alias

```zsh
alias htop='python3 ~/Developer/sysmon/sysmon.py'
```
