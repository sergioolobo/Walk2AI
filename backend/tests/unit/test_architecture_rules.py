"""Architecture rules enforced as tests, by reading the source.

These keep the properties the wallet depends on from eroding quietly:
  * no floating point in any code that handles credits or money;
  * no code outside the wallet package writing wallet tables;
  * no provider credentials outside the settings module;
  * services never commit (the caller owns the transaction);
  * business logic never reads the wall clock directly.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"

# Packages in which a float could silently corrupt an amount. Extended as the reward
# engine (Phase 2) and the pricing engine (Phase 3) arrive.
MONEY_PACKAGES = (
    APP / "services" / "wallet",
    APP / "services" / "rewards",
    APP / "services" / "ai",
)


def python_files(root: Path) -> Iterator[Path]:
    yield from sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def relative(path: Path) -> str:
    return str(path.relative_to(APP.parent))


def test_the_source_tree_is_where_these_rules_expect_it() -> None:
    assert (APP / "services" / "wallet" / "service.py").is_file()
    assert len(list(python_files(APP))) > 30


@pytest.mark.parametrize("package", MONEY_PACKAGES, ids=lambda p: p.name)
def test_money_code_contains_no_floating_point(package: Path) -> None:
    offences: list[str] = []
    for path in python_files(package):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offences.append(f"{relative(path)}:{node.lineno} float literal {node.value!r}")
            elif isinstance(node, ast.Name) and node.id == "float":
                offences.append(f"{relative(path)}:{node.lineno} use of float")
    assert offences == [], "floating point in money code:\n" + "\n".join(offences)


@pytest.mark.parametrize("package", MONEY_PACKAGES, ids=lambda p: p.name)
def test_true_division_in_money_code_is_only_ever_on_decimals(package: Path) -> None:
    """``/`` on two ints yields a float. Money code divides only Decimals, explicitly."""
    offences: list[str] = []
    for path in python_files(package):
        source = path.read_text()
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                line = lines[node.lineno - 1]
                if "Decimal(" not in line:
                    offences.append(f"{relative(path)}:{node.lineno} {line.strip()}")
    assert offences == [], "non-Decimal division in money code:\n" + "\n".join(offences)


def test_the_division_rule_actually_sees_the_one_real_division() -> None:
    """Guards the guard: the credits -> EUR display conversion is found and is Decimal."""
    source = (APP / "services" / "wallet" / "queries.py").read_text()
    divisions = [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
    ]
    assert len(divisions) == 1
    assert "Decimal(credits) / wc_per_eur" in source.splitlines()[divisions[0] - 1]


WALLET_WRITE_MARKERS = (
    "WalletTransaction(",
    "WalletReservation(",
    "WalletAccount(",
    ".available_balance =",
    ".available_balance +=",
    ".available_balance -=",
    ".reserved_balance =",
    ".reserved_balance +=",
    ".reserved_balance -=",
    ".lifetime_earned =",
    ".lifetime_earned +=",
    ".lifetime_spent =",
    ".lifetime_spent +=",
)


def test_only_the_wallet_service_writes_wallet_tables() -> None:
    allowed = {APP / "services" / "wallet" / "service.py"}
    offences = [
        f"{relative(path)}: {marker}"
        for path in python_files(APP)
        if path not in allowed and APP / "models" not in path.parents
        for marker in WALLET_WRITE_MARKERS
        if marker in path.read_text()
    ]
    assert offences == [], "wallet tables written outside WalletService:\n" + "\n".join(offences)


def test_provider_credentials_are_read_only_through_settings() -> None:
    """API keys live in one typed settings object; nothing else reads the environment."""
    offences: list[str] = []
    for path in python_files(APP):
        text = path.read_text()
        if path != APP / "core" / "config.py":
            for marker in ("os.environ", "os.getenv", "getenv("):
                if marker in text:
                    offences.append(f"{relative(path)}: reads the environment via {marker}")
        if APP / "api" in path.parents or APP / "schemas" in path.parents:
            for marker in ("openai_api_key", "anthropic_api_key", "jwt_secret"):
                if marker in text:
                    offences.append(f"{relative(path)}: HTTP layer references {marker}")
    assert offences == []


def test_services_never_commit() -> None:
    """Routers own the transaction. The reservation sweeper is the one sanctioned owner
    inside ``services`` and it uses ``session.begin()`` blocks, not commit()."""
    offences = [
        f"{relative(path)}:{number}"
        for path in python_files(APP / "services")
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if ".commit()" in line
    ]
    assert offences == []


def test_business_logic_does_not_read_the_wall_clock() -> None:
    """Time comes from the injected Clock so expiry and day boundaries are testable."""
    allowed = {APP / "core" / "clock.py", APP / "models" / "base.py", APP / "core" / "logging.py"}
    offences = [
        f"{relative(path)}:{number}: {line.strip()}"
        for path in python_files(APP)
        if path not in allowed
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if "datetime.now(" in line or "datetime.utcnow(" in line or "date.today(" in line
    ]
    assert offences == []


def test_admin_router_enforces_the_admin_role_for_every_route() -> None:
    source = (APP / "api" / "admin.py").read_text()
    assert "dependencies=[Depends(require_admin)]" in source
