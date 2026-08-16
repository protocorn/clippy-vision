"""
Quick test script to verify Clippy Vision installation
Run this after setup to ensure everything is working correctly
"""

import os
import platform
import sys


def test_imports():
    """Test if all required Python packages are installed"""
    print("\n[1/5] Testing Python imports...")
    try:
        if platform.system() == "Windows":
            import uiautomation
            import win32api
            import win32gui
        import mss
        import psutil
        from PIL import Image
        from pynput import keyboard
        print("  ✓ All Python packages imported successfully")
        return True
    except ImportError as e:
        print(f"  ✗ Import error: {e}")
        print("  Run: pip install -r requirements.txt")
        return False

def test_ollama_connection():
    """Test the bundled local embedding path."""
    print("\n[2/5] Testing local embeddings...")
    try:
        from core.local_embeddings import embed_text
        result = embed_text("test")
        if result:
            print("  ✓ Local embeddings available")
            return True
        print("  ✗ Local embeddings returned an empty vector")
        return False
    except Exception as e:
        print(f"  ✗ Local embeddings failed: {e}")
        return False

def test_models():
    """Check that the local Ollama chat model is installed."""
    print("\n[3/5] Checking local chat model...")
    try:
        import json
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=10) as response:
            payload = json.loads(response.read())
        models = [str(item.get("name") or "") for item in payload.get("models", [])]
        if any(name == "qwen3:8b" or name.startswith("qwen3:8b-") for name in models):
            print("  ✓ qwen3:8b")
            return True
        print("  ✗ qwen3:8b is not installed")
        return False
    except Exception as e:
        print(f"  ✗ Error checking models: {e}")
        return False

def test_directories():
    """Test if required directories exist"""
    print("\n[4/5] Checking project directories...")
    dirs = [
        "core/data",
        "core/data/screenshots",
        "logs"
    ]
    
    all_exist = True
    for dir_path in dirs:
        if os.path.exists(dir_path):
            print(f"  ✓ {dir_path}")
        else:
            print(f"  ✗ {dir_path} (missing)")
            all_exist = False
    
    if not all_exist:
        print("  Run setup script to create missing directories")
    return all_exist

def test_database():
    """Test database initialization"""
    print("\n[5/5] Testing database...")
    try:
        from core.storage import conn
        
        # Check if main tables exist
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row[0] for row in cursor.fetchall()]
        
        required_tables = ["events", "sessions", "memory_clusters", "memory_facts", "conversations"]
        missing_tables = [t for t in required_tables if t not in tables]
        
        if missing_tables:
            print(f"  ✗ Missing tables: {missing_tables}")
            return False
        
        print(f"  ✓ Database initialized with {len(tables)} tables")
        
        # Check event count
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print(f"  ✓ Database contains {event_count} events")
        
        return True
    except Exception as e:
        print(f"  ✗ Database error: {e}")
        return False

def main():
    print("=" * 50)
    print("  Clippy Vision - Installation Test")
    print("=" * 50)
    
    results = []
    
    results.append(("Python Imports", test_imports()))
    results.append(("Ollama Connection", test_ollama_connection()))
    results.append(("AI Models", test_models()))
    results.append(("Directories", test_directories()))
    results.append(("Database", test_database()))
    
    print("\n" + "=" * 50)
    print("  Test Summary")
    print("=" * 50)
    
    for test_name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}  {test_name}")
    
    all_passed = all(passed for _, passed in results)
    
    if all_passed:
        print("\n✓ All tests passed! Clippy Vision is ready to use.")
        print("\nNext steps:")
        print(f"  1. Start capture: python core{os.sep}screen_capture.py")
        print(f"  2. Chat with Clippy: python agent{os.sep}react_agent.py")
    else:
        print("\n✗ Some tests failed. Please fix the issues above.")
        print("  See QUICKSTART.md for troubleshooting help.")
        sys.exit(1)

if __name__ == "__main__":
    main()
