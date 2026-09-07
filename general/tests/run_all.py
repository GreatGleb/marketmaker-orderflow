"""Запускает все проверки из каталога и печатает сводку."""
import pathlib
import subprocess
import sys

TESTS_DIR = pathlib.Path(__file__).parent


def main() -> int:
    scripts = sorted(p.stem for p in TESTS_DIR.glob("test_*.py"))
    failed = []

    for name in scripts:
        result = subprocess.run(
            [sys.executable, "-m", f"tests.{name}"],
            capture_output=True, text=True,
        )

        if result.returncode:
            failed.append(name)
            print(f"УПАЛ    {name}")
            for line in result.stderr.strip().splitlines()[-3:]:
                print(f"        {line}")
        else:
            print(f"прошёл  {name}")

    print(f"\nвсего {len(scripts)}, упало {len(failed)}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
