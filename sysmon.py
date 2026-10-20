#!/usr/bin/env python3
"""
sysmon.py — htop-style terminal system monitor
Run: python3 ~/sysmon.py
"""

import curses
import psutil
import signal
import time
import os
from datetime import datetime, timedelta
from collections import deque

HISTORY_LEN    = 50
CPU_HOG_THRESH = 20.0   # %
MEM_HOG_THRESH = 3.0    # %
DUPE_COUNT_MIN = 3      # instances of same name
DUPE_RSS_MIN   = 200 * 1024 * 1024  # combined RSS bytes

# ── Helpers ───────────────────────────────────────────────────────────────────

def b2h(n):
    for unit in ['B', 'K', 'M', 'G', 'T']:
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"

def sparkline(history, width):
    chars = ' ▁▂▃▄▅▆▇█'
    recent = list(history)[-width:]
    result = ''.join(chars[min(int(v / 100 * 8), 8)] for v in recent)
    return result.rjust(width)

def safe_addstr(win, y, x, s, attr=0):
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x < 0 or x >= max_x:
        return
    s = s[:max_x - x]
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass

def safe_addch(win, y, x, ch, attr=0):
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y - 1 or x < 0 or x >= max_x - 1:
        return
    try:
        win.addch(y, x, ch, attr)
    except curses.error:
        pass

def draw_bar(win, y, x, pct, width, C):
    filled = min(int(pct * width / 100), width)
    for i in range(width):
        if i < filled:
            pos = i / max(width - 1, 1)
            if pos < 0.5:
                attr = C[2] | curses.A_BOLD
            elif pos < 0.75:
                attr = C[3] | curses.A_BOLD
            else:
                attr = C[4] | curses.A_BOLD
            safe_addch(win, y, x + i, '█', attr)
        else:
            safe_addch(win, y, x + i, '░', C[1])

def pct_color(pct, C):
    if pct < 50:
        return C[2]
    elif pct < 80:
        return C[3]
    return C[4]

def find_hogs(procs):
    seen_pids = set()
    candidates = []

    for p in procs:
        pid = p.get('pid')
        if p.get('status') == 'zombie' and pid not in seen_pids:
            candidates.append({'reason': 'ZOMBIE', 'pids': [pid], 'proc': p})
            seen_pids.add(pid)

    for p in procs:
        pid = p.get('pid')
        if (p.get('cpu_percent') or 0) > CPU_HOG_THRESH and pid not in seen_pids:
            candidates.append({'reason': 'CPU HOG', 'pids': [pid], 'proc': p})
            seen_pids.add(pid)

    for p in procs:
        pid = p.get('pid')
        if (p.get('memory_percent') or 0) > MEM_HOG_THRESH and pid not in seen_pids:
            candidates.append({'reason': 'MEM HOG', 'pids': [pid], 'proc': p})
            seen_pids.add(pid)

    # Duplicate process groups — keep lowest PID, flag rest
    from collections import defaultdict
    groups = defaultdict(list)
    for p in procs:
        groups[p.get('name') or ''].append(p)
    for name, group in groups.items():
        if len(group) < DUPE_COUNT_MIN:
            continue
        rss_list = [p['memory_info'].rss for p in group if p.get('memory_info')]
        if sum(rss_list) < DUPE_RSS_MIN:
            continue
        group.sort(key=lambda p: p.get('pid') or 0)
        kill_group = group[1:]
        pids = [p['pid'] for p in kill_group if p.get('pid') not in seen_pids]
        if pids:
            candidates.append({
                'reason': f'DUPE×{len(group)}',
                'pids': pids,
                'proc': group[0],
                'combined_rss': sum(rss_list),
            })
            seen_pids.update(pids)

    return candidates

def draw_sweep(win, candidates, sweep_sel, sweep_checked, C, max_y, max_x):
    if not candidates:
        msg = "  No hogs found — system looks clean  "
        w = len(msg) + 2
        y = max_y // 2 - 1
        x = max(0, (max_x - w) // 2)
        attr = C[2] | curses.A_REVERSE | curses.A_BOLD
        safe_addstr(win, y,     x, ' ' * w, attr)
        safe_addstr(win, y + 1, x, msg,     attr)
        safe_addstr(win, y + 2, x, ' ' * w, attr)
        safe_addstr(win, y + 3, x, '  [n] close  '.center(w), attr)
        return

    title = f" HOG SWEEP — {len(candidates)} candidates "
    w    = max(len(title) + 4, min(max_x - 4, 72))
    h    = len(candidates) + 5
    y0   = max(1, max_y // 2 - h // 2)
    x0   = max(0, (max_x - w) // 2)

    for dy in range(h):
        safe_addstr(win, y0 + dy, x0, ' ' * w, C[1] | curses.A_REVERSE)

    safe_addstr(win, y0,     x0, title.center(w), C[5] | curses.A_REVERSE | curses.A_BOLD)
    safe_addstr(win, y0 + 1, x0, '─' * w,         C[6] | curses.A_REVERSE)

    for i, cand in enumerate(candidates):
        r      = y0 + 2 + i
        check  = '✓' if i in sweep_checked else ' '
        reason = cand['reason'][:8]
        name   = (cand['proc'].get('name') or '?')[:16]
        cpu_p  = cand['proc'].get('cpu_percent') or 0
        mem_p  = cand['proc'].get('memory_percent') or 0
        if 'combined_rss' in cand:
            detail = f"combined {b2h(cand['combined_rss'])}, {len(cand['pids'])} procs"
        else:
            detail = f"PID {cand['pids'][0]}  CPU {cpu_p:.1f}%  MEM {mem_p:.1f}%"
        line = f" [{check}] {reason:<8}  {name:<16}  {detail}"
        if i == sweep_sel:
            attr = C[8] | curses.A_BOLD
        elif i in sweep_checked:
            attr = C[4] | curses.A_REVERSE
        else:
            attr = C[1] | curses.A_REVERSE
        safe_addstr(win, r, x0, line[:w].ljust(w), attr)

    fr = y0 + 2 + len(candidates)
    safe_addstr(win, fr,     x0, '─' * w, C[6] | curses.A_REVERSE)
    safe_addstr(win, fr + 1, x0,
                ' [↑↓] nav  [space] toggle  [a] all  [k] kill checked  [n] cancel '.center(w),
                C[5] | curses.A_REVERSE | curses.A_BOLD)

def divider(win, row, label, C, max_x):
    pad = max(0, max_x - len(label) - 4)
    safe_addstr(win, row, 0, f"── {label} " + "─" * pad, C[6])

def draw_kill_confirm(win, proc, sig, err, C, max_y, max_x):
    name = (proc.get('name') or '?')[:30]
    pid  = proc.get('pid', 0)
    sig_name = 'SIGKILL' if sig == signal.SIGKILL else 'SIGTERM'
    if err:
        body = f" Error: {err[:50]} "
    else:
        body = f" Kill {name} (PID {pid}) with {sig_name}?  [y] yes  [n] cancel "
    w = len(body) + 2
    y = max_y // 2 - 1
    x = max(0, (max_x - w) // 2)
    attr = C[4] | curses.A_REVERSE | curses.A_BOLD
    safe_addstr(win, y,     x, ' ' * w, attr)
    safe_addstr(win, y + 1, x, body,    attr)
    safe_addstr(win, y + 2, x, ' ' * w, attr)

# ── Main ──────────────────────────────────────────────────────────────────────

def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(800)

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_WHITE, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_RED, -1)
    curses.init_pair(5, curses.COLOR_CYAN, -1)
    curses.init_pair(6, curses.COLOR_BLUE, -1)
    curses.init_pair(7, curses.COLOR_MAGENTA, -1)
    curses.init_pair(8, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_GREEN)

    C = {i: curses.color_pair(i) for i in range(1, 10)}

    cpu_count = psutil.cpu_count(logical=True)
    cpu_hist = [deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN) for _ in range(cpu_count)]
    net_tx_hist = deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
    net_rx_hist = deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)

    selected = 0
    scroll = 0
    sort_key = 'cpu'
    pending_kill  = None
    kill_sig      = None
    kill_err      = None
    sweep_mode    = False
    sweep_cands   = []
    sweep_sel     = 0
    sweep_checked = set()
    procs = []

    prev_net = psutil.net_io_counters()
    prev_t = time.time()
    boot_t = psutil.boot_time()

    # Prime cpu_percent (first call returns 0)
    psutil.cpu_percent(percpu=True)

    while True:
        key = stdscr.getch()

        if sweep_mode:
            if key == curses.KEY_DOWN:
                sweep_sel = min(sweep_sel + 1, len(sweep_cands) - 1)
            elif key == curses.KEY_UP:
                sweep_sel = max(sweep_sel - 1, 0)
            elif key == ord(' '):
                if sweep_sel in sweep_checked:
                    sweep_checked.discard(sweep_sel)
                else:
                    sweep_checked.add(sweep_sel)
            elif key == ord('a'):
                if sweep_checked == set(range(len(sweep_cands))):
                    sweep_checked.clear()
                else:
                    sweep_checked = set(range(len(sweep_cands)))
            elif key == ord('k') and sweep_checked:
                for i in sweep_checked:
                    for pid in sweep_cands[i]['pids']:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except (ProcessLookupError, PermissionError):
                            pass
                sweep_mode = False
                sweep_checked.clear()
            elif key in (ord('n'), 27):
                sweep_mode = False
                sweep_checked.clear()
        elif pending_kill is not None:
            if key == ord('y'):
                try:
                    os.kill(pending_kill['pid'], kill_sig)
                except (ProcessLookupError, PermissionError) as e:
                    kill_err = str(e)
                else:
                    pending_kill = None
            elif key in (ord('n'), 27):
                pending_kill = None
                kill_err = None
        else:
            if key in (ord('q'), 27):
                break
            elif key == curses.KEY_DOWN:
                selected += 1
            elif key == curses.KEY_UP:
                selected = max(0, selected - 1)
            elif key == ord('c'):
                sort_key = 'cpu'
            elif key == ord('m'):
                sort_key = 'mem'
            elif key == ord('p'):
                sort_key = 'pid'
            elif key == ord('n'):
                sort_key = 'name'
            elif key == ord('t'):
                selected = 0
                scroll = 0
            elif key == ord('H'):
                sweep_cands   = find_hogs(procs)
                sweep_sel     = 0
                sweep_checked = set()
                sweep_mode    = True
            elif key == ord('k') and procs:
                pending_kill = procs[selected].copy()
                kill_sig = signal.SIGTERM
                kill_err = None
            elif key == ord('K') and procs:
                pending_kill = procs[selected].copy()
                kill_sig = signal.SIGKILL
                kill_err = None

        max_y, max_x = stdscr.getmaxyx()
        if max_x < 60 or max_y < 15:
            stdscr.erase()
            safe_addstr(stdscr, 0, 0, "Terminal too small!", C[4] | curses.A_BOLD)
            stdscr.refresh()
            continue

        stdscr.erase()

        # ── Gather ────────────────────────────────────────────────────────
        cpu_per = psutil.cpu_percent(percpu=True)
        cpu_total = sum(cpu_per) / len(cpu_per) if cpu_per else 0
        for i, p in enumerate(cpu_per):
            if i < len(cpu_hist):
                cpu_hist[i].append(p)

        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        curr_net = psutil.net_io_counters()
        curr_t = time.time()
        dt = max(curr_t - prev_t, 0.001)
        tx_rate = (curr_net.bytes_sent - prev_net.bytes_sent) / dt
        rx_rate = (curr_net.bytes_recv - prev_net.bytes_recv) / dt
        prev_net, prev_t = curr_net, curr_t

        # Cap sparkline at 10 MB/s for scale
        net_tx_hist.append(min(tx_rate / (10 * 1024 * 1024) * 100, 100))
        net_rx_hist.append(min(rx_rate / (10 * 1024 * 1024) * 100, 100))

        uptime = str(timedelta(seconds=int(time.time() - boot_t)))
        hostname = os.uname().nodename
        now_str = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")

        # Processes
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent',
                                       'status', 'num_threads', 'username', 'memory_info']):
            try:
                procs.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        procs.sort(key=lambda x: (-(x.get('cpu_percent') or 0) if sort_key == 'cpu'
                                   else -(x.get('memory_percent') or 0) if sort_key == 'mem'
                                   else (x.get('pid') or 0) if sort_key == 'pid'
                                   else (x.get('name') or '').lower()))

        selected = max(0, min(selected, len(procs) - 1))

        # ── Header ────────────────────────────────────────────────────────
        row = 0
        hdr = f"  ⚡ SYSMON  │  {hostname}  │  up {uptime}  │  {now_str}  │  CPU {cpu_total:.1f}%  "
        safe_addstr(stdscr, row, 0, hdr.ljust(max_x),
                    C[5] | curses.A_BOLD | curses.A_REVERSE)
        row += 1

        # ── CPU ───────────────────────────────────────────────────────────
        divider(stdscr, row, "CPU", C, max_x)
        row += 1

        ncols = 2 if cpu_count > 4 else 1
        col_w = max_x // ncols
        bar_w = col_w - 22

        for i, pct in enumerate(cpu_per[:min(cpu_count, 16)]):
            col = i % ncols
            r = row + i // ncols
            x = col * col_w
            if r >= max_y - 8:
                break
            label = f"  CPU{i:<2} "
            safe_addstr(stdscr, r, x, label, C[5] | curses.A_BOLD)
            safe_addstr(stdscr, r, x + len(label), "[", C[1])
            draw_bar(stdscr, r, x + len(label) + 1, pct, bar_w, C)
            safe_addstr(stdscr, r, x + len(label) + 1 + bar_w, "]", C[1])
            safe_addstr(stdscr, r, x + len(label) + bar_w + 3,
                        f"{pct:5.1f}%", pct_color(pct, C) | curses.A_BOLD)
            spark = sparkline(cpu_hist[i], 8)
            safe_addstr(stdscr, r, x + len(label) + bar_w + 10, spark, C[6])

        row += (min(cpu_count, 16) + ncols - 1) // ncols + 1

        # ── Memory / Swap ─────────────────────────────────────────────────
        if row < max_y - 10:
            divider(stdscr, row, "MEMORY", C, max_x)
            row += 1
            mbar_w = max_x - 32

            for label, total, used, pct in [
                ("  Mem  ", mem.total, mem.used, mem.percent),
                ("  Swap ", swap.total, swap.used, swap.percent),
            ]:
                if row >= max_y - 8:
                    break
                safe_addstr(stdscr, row, 0, label, C[5] | curses.A_BOLD)
                safe_addstr(stdscr, row, len(label), "[", C[1])
                draw_bar(stdscr, row, len(label) + 1, pct, mbar_w, C)
                safe_addstr(stdscr, row, len(label) + 1 + mbar_w, "]", C[1])
                info = f" {b2h(used)}/{b2h(total)} ({pct:.1f}%)"
                safe_addstr(stdscr, row, len(label) + 3 + mbar_w, info,
                            pct_color(pct, C) | curses.A_BOLD)
                row += 1

        # ── Network ───────────────────────────────────────────────────────
        if row < max_y - 8:
            divider(stdscr, row, "NETWORK", C, max_x)
            row += 1
            tx_spark = sparkline(net_tx_hist, 16)
            rx_spark = sparkline(net_rx_hist, 16)
            net_line = (f"  ↑ {b2h(tx_rate)}/s  {tx_spark}"
                        f"    ↓ {b2h(rx_rate)}/s  {rx_spark}")
            safe_addstr(stdscr, row, 0, net_line, C[7] | curses.A_BOLD)
            row += 1

        # ── Processes ─────────────────────────────────────────────────────
        if row < max_y - 4:
            divider(stdscr, row,
                    f"PROCESSES  {len(procs)} total  [sort: {sort_key.upper()}  c/m/p/n]",
                    C, max_x)
            row += 1

            col_hdr = f"{'PID':>7}  {'CPU%':>5}  {'MEM%':>5}  {'MEM':>6}  {'VIRT':>7}  {'RES':>7}  {'THR':>4}  {'ST':<6}  {'USER':<9}  COMMAND"
            safe_addstr(stdscr, row, 0, col_hdr[:max_x],
                        C[5] | curses.A_BOLD | curses.A_UNDERLINE)
            row += 1

            avail_rows = max_y - row - 2
            if selected >= scroll + avail_rows:
                scroll = selected - avail_rows + 1
            if selected < scroll:
                scroll = selected

            for i, proc in enumerate(procs[scroll:scroll + avail_rows]):
                if row >= max_y - 2:
                    break
                real_i = i + scroll

                pid   = proc.get('pid') or 0
                name  = (proc.get('name') or '?')[:20]
                cpu_p = proc.get('cpu_percent') or 0.0
                mem_p = proc.get('memory_percent') or 0.0
                st    = (proc.get('status') or '?')[:6]
                user  = (proc.get('username') or '?')[:9]
                thr   = proc.get('num_threads') or 0
                mi    = proc.get('memory_info')
                virt  = b2h(mi.vms) if mi else '?'
                res   = b2h(mi.rss) if mi else '?'
                mem_g = f"{mi.rss / (1024**3):.2f}G" if mi else '?'

                line = f"{pid:>7}  {cpu_p:>5.1f}  {mem_p:>5.1f}  {mem_g:>6}  {virt:>7}  {res:>7}  {thr:>4}  {st:<6}  {user:<9}  {name}"

                if real_i == selected:
                    attr = C[8] | curses.A_BOLD
                elif cpu_p > 50:
                    attr = C[4]
                elif cpu_p > 10:
                    attr = C[3]
                else:
                    attr = C[1]

                if real_i == selected:
                    safe_addstr(stdscr, row, 0, line[:max_x].ljust(max_x), attr)
                else:
                    safe_addstr(stdscr, row, 0, line[:max_x], attr)
                row += 1

        # ── Footer ────────────────────────────────────────────────────────
        footer = "  [q] quit   [↑↓] scroll   [t] top   [k] kill   [K] SIGKILL   [H] hog sweep   [c/m/p/n] sort  "
        safe_addstr(stdscr, max_y - 1, 0, footer.ljust(max_x),
                    C[5] | curses.A_REVERSE)

        if sweep_mode:
            draw_sweep(stdscr, sweep_cands, sweep_sel, sweep_checked, C, max_y, max_x)
        elif pending_kill is not None:
            draw_kill_confirm(stdscr, pending_kill, kill_sig, kill_err, C, max_y, max_x)

        stdscr.refresh()

if __name__ == '__main__':
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
