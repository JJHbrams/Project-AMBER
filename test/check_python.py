import sys
import platform

def check_python():
    print("=" * 40)
    print(f"Python 버전:    {sys.version}")
    print(f"버전 튜플:      {sys.version_info[:3]}")
    print(f"실행 경로:      {sys.executable}")
    print(f"플랫폼:         {platform.platform()}")
    print(f"아키텍처:       {platform.machine()}")
    print("=" * 40)

    major, minor, _ = sys.version_info[:3]
    if (major, minor) >= (3, 11):
        print("✅ Python 3.11+ — 조건 충족")
    else:
        print(f"⚠️  Python {major}.{minor} — 3.11 미만")

if __name__ == "__main__":
    check_python()
