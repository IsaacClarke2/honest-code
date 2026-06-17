# Demo

`tamper_demo.sh` is a self-contained demonstration of the tamper-detection
backstop. It builds a throwaway `$HOME` and git project in a temp dir, so it
touches **nothing** in your real `~/.claude`.

```bash
bash demo/tamper_demo.sh
```

It walks through four steps:

1. An honest session — the checker computes and **signs** the score.
2. The agent secretly rewrites its own score to `0.99`.
3. The next command — `enforce.py` sees the broken signature and **freezes** git writes.
4. The checker runs — the tamper is **logged**, the score is **penalized** (`-0.25`), and the status line turns red `⚠TAMPER`.

## Recording the GIF

With [vhs](https://github.com/charmbracelet/vhs) installed:

```bash
vhs demo/demo.tape   # writes demo.gif
```

That GIF is the share-worthy moment: an agent forging its own honesty score, and
getting caught in real time.
