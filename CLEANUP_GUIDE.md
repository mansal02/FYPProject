# Cleanup Guide - Remove Redundant Documentation

## Delete These Files (Consolidated into OPTIMIZATION.md)

Run this to delete:

```bash
rm -f \
  START_HERE.md \
  SPEED_SOLUTION_TL_DR.md \
  ROOT_CAUSE_ANALYSIS.md \
  WHY_SLOW_AND_HOW_TO_FIX.md \
  RESPONSE_SPEED_SOLUTION_COMPLETE.md \
  DELIVERY_SUMMARY.md \
  PARALLEL_OPTIMIZATION_GUIDE.md \
  PARALLEL_OPTIMIZATION_QUICK_REF.md \
  PARALLEL_OPTIMIZATION_COMPLETE.md \
  DOCUMENTATION_INDEX.md \
  PACKAGE_INDEX.md \
  VERIFICATION_CHECKLIST.md
```

Or on Windows (PowerShell):

```powershell
$files = @(
  "START_HERE.md",
  "SPEED_SOLUTION_TL_DR.md",
  "ROOT_CAUSE_ANALYSIS.md",
  "WHY_SLOW_AND_HOW_TO_FIX.md",
  "RESPONSE_SPEED_SOLUTION_COMPLETE.md",
  "DELIVERY_SUMMARY.md",
  "PARALLEL_OPTIMIZATION_GUIDE.md",
  "PARALLEL_OPTIMIZATION_QUICK_REF.md",
  "PARALLEL_OPTIMIZATION_COMPLETE.md",
  "DOCUMENTATION_INDEX.md",
  "PACKAGE_INDEX.md",
  "VERIFICATION_CHECKLIST.md"
)

foreach ($file in $files) {
  if (Test-Path $file) {
    Remove-Item $file -Force
    Write-Host "Deleted: $file"
  }
}
```

---

## Keep These Files

### Documentation
- `README.md` - Main project readme
- `OPTIMIZATION.md` - **NEW** Consolidated optimization guide
- `WORKER_ORCHESTRATOR_GUIDE.md` - Architecture docs
- `UAT_REPORT_2026-05-12.md` - Testing/validation
- `docs/SYSTEM_OVERVIEW.md` - System architecture
- `knowledge/memory_agent/README.md` - Memory module docs

### Configuration
- `config.yaml` - App configuration
- `requirements.txt` - **UPDATED** Python dependencies with comments
- `ollama_models.txt` - **NEW** Ollama model pull commands

### Code
- `aiassistant/` - All source code (no changes)
- `models/` - Avatar models (keep as-is)
- `piper/` - Voice models (keep as-is)
- `rvc_models/` - Voice conversion (keep as-is)

### Scripts/Examples
- `marie.bat` - Main launcher
- `example_worker_usage.py` - Usage example
- `example_ultra_fast_usage.py` - Speed demo
- `test_fast_integration.py` - Integration test

---

## What Changed

### ✅ requirements.txt
- Added section headers with descriptions
- Organized by category (Core, LLM, Office, System, etc.)
- Added installation notes
- Added notes about GPU/voice features

### ✅ OPTIMIZATION.md (NEW)
- Single consolidated guide
- Quick start instructions
- Performance metrics
- Configuration guide
- Troubleshooting section

### ✅ ollama_models.txt (NEW)
- All required Ollama models
- Pull commands ready to copy
- System requirements
- Quick start guide

### ✅ aiassistant/launchers/runsys.py
- Added `MARIE_DISABLE_FAST_ORCHESTRATOR=1` (safe default)

### ✅ aiassistant/core/agent_core.py
- Fast orchestrator integration
- 5-second timeout to prevent hanging
- Better error logging

---

## Project Structure After Cleanup

```
d:\pylearn\FYP\AiAssistant\
├── README.md                          ← Main docs
├── OPTIMIZATION.md                    ← **NEW** Speed guide
├── WORKER_ORCHESTRATOR_GUIDE.md       ← Architecture
├── UAT_REPORT_2026-05-12.md          ← Testing
├── requirements.txt                   ← **UPDATED** Dependencies
├── ollama_models.txt                  ← **NEW** Models
├── config.yaml
├── marie.bat
├── aiassistant/
│   ├── core/
│   │   ├── agent_core.py              ← **UPDATED** Fast integration
│   │   ├── ultra_fast_orchestrator.py ← Speed optimization
│   │   ├── parallel_orchestrator.py
│   │   └── ...
│   ├── workers/
│   │   ├── fast_offline_worker.py     ← Speed optimization
│   │   └── ...
│   ├── frontend/
│   ├── backend/
│   ├── infra/
│   └── ...
├── docs/
│   └── SYSTEM_OVERVIEW.md
├── knowledge/
│   └── memory_agent/
│       └── README.md
├── models/                            ← Keep all
├── piper/                             ← Keep all
├── rvc_models/                        ← Keep all
├── cache/
├── checkpoints/
├── chroma/
└── ...

# DELETED:
# START_HERE.md
# SPEED_SOLUTION_TL_DR.md
# ROOT_CAUSE_ANALYSIS.md
# ... (12 other optimization .md files)
```

---

## Installation & Setup

### 1. Update Python Packages
```bash
pip install -r requirements.txt
```

### 2. Download Ollama Models
See `ollama_models.txt` for all available models.

Minimum install:
```bash
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5vl:7b
```

With fast responses:
```bash
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5vl:7b
ollama pull qwen2.5:3b
```

### 3. Run App
```bash
python marie.bat
# or
python aiassistant/launchers/runsys.py --mode assistant
```

---

## Quick Reference

| File | Purpose | Keep/Delete |
|------|---------|------------|
| README.md | Main docs | ✅ Keep |
| OPTIMIZATION.md | Speed guide | ✅ Keep (NEW) |
| WORKER_ORCHESTRATOR_GUIDE.md | Architecture | ✅ Keep |
| requirements.txt | Dependencies | ✅ Keep (UPDATED) |
| ollama_models.txt | Models to download | ✅ Keep (NEW) |
| 12 other .md files | Optimization docs | ❌ Delete |

---

## Status

✅ Documentation consolidated  
✅ Dependencies organized with comments  
✅ Model commands centralized  
✅ Fast orchestrator disabled by default (safe)  
✅ Ready for cleanup  

---

## Next Steps

1. Delete the 12 redundant .md files (use script above)
2. Keep OPTIMIZATION.md, requirements.txt, ollama_models.txt
3. Run: `pip install -r requirements.txt`
4. Run: `ollama pull qwen2.5-coder:7b && ollama pull qwen2.5vl:7b`
5. Start app: `python marie.bat`
