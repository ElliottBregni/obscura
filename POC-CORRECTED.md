# ✅ POC: Nested Agent Routing (CORRECTED)

## Vault Structure

```
~/FV-Copilot/instructions/
├── x.md              # Universal
├── x.copilot.md      # Copilot override ✨
├── x.claude.md       # Claude override ✨
├── y.copilot.md      # Copilot-only ⭐
├── y.claude.md       # Claude-only ⭐
└── z.md              # Universal (no overrides) 🌐
```

## Pattern

**`filename.agent.ext`** overrides **`filename.ext`** in the same directory

- `x.copilot.md` → copilot sees as `x.md`
- `x.claude.md` → claude sees as `x.md`
- `x.md` → universal fallback

## What Each Agent Sees

### Copilot
```
instructions/x.md  ← x.copilot.md
instructions/y.md  ← y.copilot.md
instructions/z.md  ← z.md (universal)
```

### Claude
```
instructions/x.md  ← x.claude.md
instructions/y.md  ← y.claude.md
instructions/z.md  ← z.md (universal)
```

## Benefits

✅ **Single directory** - All files in one place  
✅ **Visual clarity** - See universal + overrides side-by-side  
✅ **No nesting** - Flat structure, easy to navigate  
✅ **Scalable** - Add more agent overrides anytime  
✅ **Clear intent** - Agent suffix shows what's customized  

## Test Files

```bash
cd ~/FV-Copilot/instructions
ls -1
```

Output:
```
x.claude.md
x.copilot.md
x.md
y.claude.md
y.copilot.md
z.md
```

**Status**: ✅ Structure created and verified  
**Next**: Test merge function with this pattern
