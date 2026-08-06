#!/usr/bin/env python
"""environment diagnostic: check if structure_extractor can run correctly"""
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))

# ── 1. Load .env ──
env_file = PROJECT_ROOT / ".env"
envbat_file = PROJECT_ROOT / ".envbat"
print(f"[1/5] Loading .env: {env_file} (exists={env_file.exists()})")
if env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file, override=True)
        print("      dotenv loaded OK")
    except ImportError:
        print("      ⚠️  python-dotenv not installed, falling back to manual parse")
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ[k.strip()] = v.strip()
        print("      manual parse done")
else:
    print(f"      ⚠️  .env NOT FOUND! Check if {envbat_file} is the real config?")

# ── 2. Check LLM env vars ──
print(f"\n[2/5] LLM Configuration:")
print(f"      FMEA_MODEL_NAME  = {os.getenv('FMEA_MODEL_NAME', '❌ NOT SET')}")
print(f"      FMEA_MODEL_BASE_URL = {os.getenv('FMEA_MODEL_BASE_URL', '❌ NOT SET')}")
print(f"      FMEA_MODEL_API_KEY  = {'✅ SET ('+str(len(os.getenv('FMEA_MODEL_API_KEY','')))+' chars)' if os.getenv('FMEA_MODEL_API_KEY') else '❌ NOT SET'}")

# ── 3. Check dependencies ──
print(f"\n[3/5] Dependency Check:")
deps = {
    "pdfplumber": False,
    "openpyxl": False,
    "pypdf": False,
    "httpx": False,
    "dotenv": False,
}
for dep in deps:
    try:
        __import__(dep)
        deps[dep] = True
    except ImportError:
        pass
for dep, ok in deps.items():
    print(f"      {dep:15s} {'✅' if ok else '❌ MISSING'}")

# ── 4. Check LLM client ──
print(f"\n[4/5] LLM Client:")
try:
    from engine.llm_client import LLMClient
    llm = LLMClient()
    print(f"      model_id: {llm.model_id}")
    print(f"      base_url: {llm.base_url}")
    print(f"      api_key:  {'✅ SET' if llm.api_key else '❌ NOT SET'}")
except Exception as e:
    print(f"      ❌ Failed to init LLMClient: {e}")

# ── 5. Quick LLM test ──
print(f"\n[5/5] LLM Connectivity Test (calling {os.getenv('FMEA_MODEL_NAME', '?')})...")
try:
    import asyncio
    async def test():
        from engine.llm_client import LLMClient
        llm = LLMClient()
        result = await llm.safe_call(
            system_prompt="只回复一个JSON: {\"response\": \"ok\"}",
            user_message="test",
            fallback_to_complete=True,
        )
        return result
    result = asyncio.run(test())
    if "ok" in str(result).lower():
        print("      ✅ LLM responded successfully")
    else:
        print(f"      ⚠️  Unexpected response: {result[:200]}")
except Exception as e:
    print(f"      ❌ LLM test failed: {e}")

print("\n" + "=" * 50)
print("Diagnostic complete. If all ✅, run:")
print(r'  D:\agent-fmea\agno\.venv\Scripts\python.exe -m knowledge.structure_extractor "C:\Users\wzjgha\Desktop\12V低压电池"')
