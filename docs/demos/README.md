# Demo GIFs

Three real crashes, each analyzed through TriagePilot's actual
`create_session` / `get_crash_info` / `run_crash_analysis` /
`locate_faulting_source` path (the same functions `analyze_dump` calls),
rendered as short terminal recordings:

| GIF | Example | What it shows |
|---|---|---|
| `use-after-free.gif` | [`use-after-free.cpp`](../../examples/common/use-after-free.cpp) | Basic signal → frame → source grounding |
| `double-free.gif` | [`double-free.cpp`](../../examples/common/double-free.cpp) | Crash location (line 59) vs. root cause (line 46) — the abort backtrace alone doesn't tell you which |
| `thread-uaf.gif` | [`thread-uaf.cpp`](../../examples/common/thread-uaf.cpp) | Cross-thread use-after-free — the crashing thread's stack only makes sense next to the thread that freed the memory |

Each `src/<name>/analysis.md` is a trimmed-but-verbatim copy of real
`analyze_dump` output — signal, faulting frame, backtrace, and source
snippet are not fabricated; only giant register dumps and repeated
noise are cut for legibility in a ~10s recording.

## Requirements

```bash
brew install vhs glow
```

`vhs` (Charm) renders a `.tape` script into a GIF; `glow` renders the
markdown analysis output with syntax highlighting inside the recording.

## Regenerate an existing demo

```bash
cd docs/demos/src/<name>
vhs demo.tape                    # writes ./demo.gif
mv demo.gif ../../<name>.gif
```

## Adding a new one

1. Build and crash another example (see `examples/macos/gen_core_mac.sh` or
   `eval/run_eval.py`'s `_run_until_crash_macos` for the technique).
2. Run it through TriagePilot's real analysis path (see
   `eval/run_eval.py`'s `evaluate_example` for the exact call sequence) and
   copy the genuine signal/frame/source/backtrace output into a new
   `src/<name>/analysis.md`.
3. Copy `src/use-after-free/run_demo.sh` and `demo.tape` as a starting
   point, adjust the narration to match the real facts in `analysis.md`,
   and render.
4. Add a row to the table above and link the new GIF from the main
   [`README.md`](../../README.md).

Keep the analysis content real — the whole point of these demos is that
the output is grounded in an actual debugger session, not written copy.
