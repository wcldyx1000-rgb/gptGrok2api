import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PostgresDeployScaffoldTest(unittest.TestCase):
    def test_postgres_compose_overlay_wires_app_to_database(self) -> None:
        text = (ROOT / "docker-compose.postgres.yml").read_text(encoding="utf-8")
        self.assertIn("postgres:18-alpine", text)
        self.assertIn("POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD", text)
        self.assertIn("STORAGE_BACKEND: postgres", text)
        self.assertIn("@postgres:5432/", text)
        self.assertIn("service_healthy", text)
        # 密码必须由 .env 提供，不允许硬编码默认值。
        self.assertNotIn("POSTGRES_PASSWORD:-", text)

    def test_install_script_exposes_database_modes(self) -> None:
        text = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
        for token in ("--database)", "--postgres-password)", "postgres-local", "postgres-url", "docker-compose.postgres.yml"):
            self.assertIn(token, text)
        # 默认存储后端保持 json。
        self.assertIn('STORAGE_BACKEND="${STORAGE_BACKEND:-json}"', text)

    def test_install_script_syntax(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not available")
        # 用相对路径 + cwd：Windows 上的 bash（WSL/Git Bash）不认反斜杠绝对路径。
        result = subprocess.run([bash, "-n", "deploy/install.sh"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
