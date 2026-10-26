"""
插件市场服务单元测试

覆盖：克隆添加（成功/查重/非法地址/失败不保留记录）、同步（远端更新生效、
目录丢失自动重克隆）、浏览（marketplace.json 解析 + 防穿越 + 无清单扫描兜底）、
删除（目录清理）、评测期懒加载（节流窗口/远端更新生效/失败降级/版本锚点）。
git 操作走本地临时仓库（file:// 克隆，无网络）。
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.plugin_marketplace_models import PluginMarketplace
from app.services import plugin_marketplace_service as svc
from app.services.plugin_marketplace_service import (
    MarketplaceError,
    _safe_resolve,
    create_marketplace,
    delete_marketplace,
    list_marketplace_plugins,
    list_marketplaces,
    prepare_market_plugin_for_eval,
    sync_marketplace,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（本地 git + sqlite，无网络/无外部 DB）


GOOD_SKILL_MD = """---
name: market-skill
description: Use when the user asks to run the market skill demo.
---

# Market Skill

Run it.
"""


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


@pytest.fixture
def db_session(tmp_path):
    db_file = tmp_path / "market.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["plugin_marketplaces"]])
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def marketplace_base(tmp_path, monkeypatch):
    """市场克隆根目录指向临时目录，避免污染工作区 uploads/"""
    base = tmp_path / "marketplaces"
    base.mkdir()
    monkeypatch.setattr(svc, "MARKETPLACE_BASE_DIR", base)
    return base


@pytest.fixture
def remote_marketplace(tmp_path):
    """本地"远端"市场仓库：marketplace.json 列一个插件（含 skills）+ 一个独立 skill"""
    remote = tmp_path / "remote-market"
    (remote / ".claude-plugin").mkdir(parents=True)
    (remote / "public" / "anc-demo").mkdir(parents=True)
    (remote / "public" / "anc-demo" / ".claude-plugin").mkdir()
    (remote / "public" / "anc-demo" / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "anc-demo", "description": "demo plugin"}), encoding="utf-8")
    (remote / "public" / "anc-demo" / "skills" / "inner-skill").mkdir(parents=True)
    (remote / "public" / "anc-demo" / "skills" / "inner-skill" / "SKILL.md").write_text(GOOD_SKILL_MD, encoding="utf-8")
    (remote / "skills" / "standalone-skill").mkdir(parents=True)
    (remote / "skills" / "standalone-skill" / "SKILL.md").write_text(GOOD_SKILL_MD, encoding="utf-8")
    (remote / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "test-market",
        "plugins": [
            {"name": "anc-demo", "description": "demo plugin", "version": "1.0.0", "source": "./public/anc-demo"},
            {"name": "standalone", "description": "", "source": "./skills/standalone-skill"},
            {"name": "escaped", "description": "", "source": "../../outside"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    _git("init", "-b", "main", cwd=remote)
    _git("add", ".", cwd=remote)
    _git("commit", "-m", "init", cwd=remote)
    return remote


@pytest.fixture(autouse=True)
def local_session_factory(db_session, monkeypatch):
    """评测期入口（prepare_market_plugin_for_eval）的 SessionLocal 指向测试库"""
    monkeypatch.setattr(svc, "SessionLocal", lambda: db_session)
    return db_session


class TestSafeResolve:
    """source 相对路径解析与防穿越"""

    def test_normal_relative(self, tmp_path):
        (tmp_path / "public" / "anc-demo").mkdir(parents=True)
        target = _safe_resolve(tmp_path, "./public/anc-demo")
        assert target == (tmp_path / "public" / "anc-demo").resolve()

    def test_traversal_rejected(self, tmp_path):
        assert _safe_resolve(tmp_path, "../../outside") is None

    def test_dotpath_rejected(self, tmp_path):
        assert _safe_resolve(tmp_path, ".git") is None
        assert _safe_resolve(tmp_path, "..hidden") is None

    def test_empty_rejected(self, tmp_path):
        assert _safe_resolve(tmp_path, "") is None

    def test_missing_dir_rejected(self, tmp_path):
        assert _safe_resolve(tmp_path, "no/such/dir") is None


class TestGitCredentialUrl:
    """环境变量凭据注入：URL 编码、去重、非 http 跳过"""

    URL = "https://gitlab.example.com/demo-group/anc-marketplace.git"

    def test_no_env_returns_plain(self, monkeypatch):
        monkeypatch.delenv("PLUGIN_MARKET_GIT_USERNAME", raising=False)
        monkeypatch.delenv("PLUGIN_MARKET_GIT_PASSWORD", raising=False)
        assert svc._git_credential_url(self.URL) == self.URL

    def test_creds_injected_and_encoded(self, monkeypatch):
        monkeypatch.setenv("PLUGIN_MARKET_GIT_USERNAME", "gitlab-bot")
        monkeypatch.setenv("PLUGIN_MARKET_GIT_PASSWORD", "bot-secret-001")
        result = svc._git_credential_url(self.URL)
        assert result.startswith("https://gitlab-bot:bot-secret-001@gitlab.example.com/")
        assert result.endswith("anc-marketplace.git")

    def test_existing_creds_not_overridden(self, monkeypatch):
        monkeypatch.setenv("PLUGIN_MARKET_GIT_USERNAME", "u")
        monkeypatch.setenv("PLUGIN_MARKET_GIT_PASSWORD", "p")
        assert svc._git_credential_url("https://a:b@host/x.git") == "https://a:b@host/x.git"

    def test_non_http_skipped(self, monkeypatch):
        monkeypatch.setenv("PLUGIN_MARKET_GIT_USERNAME", "u")
        monkeypatch.setenv("PLUGIN_MARKET_GIT_PASSWORD", "p")
        assert svc._git_credential_url("git@host:group/x.git") == "git@host:group/x.git"


class TestCreateMarketplace:
    """添加市场：克隆、查重、非法地址、失败清理"""

    def test_create_clones_and_records(self, db_session, marketplace_base, remote_marketplace):
        record = create_marketplace(db_session, "测试市场", f"file://{remote_marketplace}")
        assert record["sync_status"] == "ok"
        assert record["last_synced_at"] is not None
        local = Path(record["local_path"])
        assert (local / ".claude-plugin" / "marketplace.json").is_file()

    def test_duplicate_url_rejected(self, db_session, marketplace_base, remote_marketplace):
        create_marketplace(db_session, "第一个", f"file://{remote_marketplace}")
        with pytest.raises(MarketplaceError, match="已添加为市场"):
            create_marketplace(db_session, "第二个", f"file://{remote_marketplace}")

    def test_invalid_url_rejected(self, db_session, marketplace_base):
        with pytest.raises(MarketplaceError, match="不合法"):
            create_marketplace(db_session, "bad", "ftp://not-a-git-url")

    def test_clone_failure_keeps_no_record(self, db_session, marketplace_base, tmp_path):
        with pytest.raises(MarketplaceError, match="克隆市场仓库失败"):
            create_marketplace(db_session, "bad", f"file://{tmp_path / 'no-such-repo'}")
        assert list_marketplaces(db_session) == []

    def test_branch_used_for_clone(self, db_session, marketplace_base, remote_marketplace):
        _git("checkout", "-b", "dev", cwd=remote_marketplace)
        record = create_marketplace(db_session, "dev市场", f"file://{remote_marketplace}", branch="dev")
        assert record["sync_status"] == "ok"


class TestSyncMarketplace:
    """同步市场：远端更新生效、目录丢失重克隆、失败状态落库"""

    def test_sync_pulls_remote_updates(self, db_session, marketplace_base, remote_marketplace):
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        # 远端新增插件并登记进 marketplace.json 后提交
        new_plugin = remote_marketplace / "public" / "anc-new"
        (new_plugin / ".claude-plugin").mkdir(parents=True)
        (new_plugin / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        manifest = remote_marketplace / ".claude-plugin" / "marketplace.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["plugins"].append({"name": "anc-new", "description": "", "source": "./public/anc-new"})
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _git("add", ".", cwd=remote_marketplace)
        _git("commit", "-m", "add anc-new", cwd=remote_marketplace)

        synced = sync_marketplace(db_session, record["id"])
        assert synced["sync_status"] == "ok"
        names = {p["name"] for p in list_marketplace_plugins(db_session, record["id"])}
        assert "anc-new" in names

    def test_sync_reclones_when_dir_missing(self, db_session, marketplace_base, remote_marketplace):
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        import shutil
        shutil.rmtree(record["local_path"])
        synced = sync_marketplace(db_session, record["id"])
        assert synced["sync_status"] == "ok"
        assert Path(synced["local_path"]).is_dir()

    def test_sync_failure_recorded(self, db_session, marketplace_base, remote_marketplace, monkeypatch):
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        monkeypatch.setattr(svc, "_fetch_and_reset", lambda *a, **k: (_ for _ in ()).throw(MarketplaceError("net down")))
        with pytest.raises(MarketplaceError, match="同步市场失败"):
            sync_marketplace(db_session, record["id"])
        rows = {m["id"]: m for m in list_marketplaces(db_session)}
        assert rows[record["id"]]["sync_status"] == "failed"
        assert "net down" in rows[record["id"]]["sync_error"]


class TestListMarketplacePlugins:
    """浏览市场：manifest 解析、插件内 skill 展开、防穿越跳过、无 manifest 扫描兜底"""

    def test_list_from_manifest(self, db_session, marketplace_base, remote_marketplace):
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        items = list_marketplace_plugins(db_session, record["id"])
        by_name = {i["name"]: i for i in items}
        # manifest 的两个合法条目（越界条目被跳过）+ 插件内 skill 展开
        assert set(by_name) == {"anc-demo", "anc-demo / inner-skill", "standalone"}
        assert by_name["anc-demo"]["target_type"] == "plugin"
        assert by_name["anc-demo"]["version"] == "1.0.0"
        assert by_name["anc-demo / inner-skill"]["target_type"] == "skill"
        assert by_name["anc-demo / inner-skill"]["source"] == "./public/anc-demo/skills/inner-skill"
        assert by_name["standalone"]["target_type"] == "skill"

    def test_plugin_skill_expansion_order_and_description(self, db_session, marketplace_base, remote_marketplace):
        """插件条目后紧跟其 skill 条目（分组顺序），且带 SKILL.md 描述"""
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        items = list_marketplace_plugins(db_session, record["id"])
        types = [(i["name"], i["target_type"]) for i in items]
        assert types.index(("anc-demo", "plugin")) + 1 == types.index(("anc-demo / inner-skill", "skill"))
        skill_item = next(i for i in items if i["name"] == "anc-demo / inner-skill")
        assert "market skill demo" in skill_item["description"]

    def test_plugin_without_skills_not_expanded(self, db_session, marketplace_base, tmp_path):
        """插件无 skills/ 目录时只出插件级条目"""
        remote = tmp_path / "no-skills-market"
        (remote / "public" / "bare" / ".claude-plugin").mkdir(parents=True)
        (remote / "public" / "bare" / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        (remote / ".claude-plugin").mkdir(parents=True)
        (remote / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
            "plugins": [{"name": "bare", "source": "./public/bare"}],
        }), encoding="utf-8")
        _git("init", "-b", "main", cwd=remote)
        _git("add", ".", cwd=remote)
        _git("commit", "-m", "init", cwd=remote)

        record = create_marketplace(db_session, "bare", f"file://{remote}")
        items = list_marketplace_plugins(db_session, record["id"])
        assert [i["name"] for i in items] == ["bare"]

    def test_skills_dir_without_skill_md_skipped(self, db_session, marketplace_base, tmp_path):
        """skills/ 下无 SKILL.md 的子目录不算可评测对象"""
        remote = tmp_path / "bad-skill-market"
        (remote / "public" / "p" / "skills" / "empty-dir").mkdir(parents=True)
        (remote / ".claude-plugin").mkdir(parents=True)
        (remote / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
            "plugins": [{"name": "p", "source": "./public/p"}],
        }), encoding="utf-8")
        _git("init", "-b", "main", cwd=remote)
        _git("add", ".", cwd=remote)
        _git("commit", "-m", "init", cwd=remote)

        record = create_marketplace(db_session, "bad", f"file://{remote}")
        # public/p 无 plugin.json 无 SKILL.md → 插件级条目被剔除，skill 也未展开
        assert list_marketplace_plugins(db_session, record["id"]) == []

    def test_list_fallback_scan_without_manifest(self, db_session, marketplace_base, tmp_path):
        remote = tmp_path / "plain-market"
        (remote / "my-skill").mkdir(parents=True)
        (remote / "my-skill" / "SKILL.md").write_text(GOOD_SKILL_MD, encoding="utf-8")
        (remote / "not-a-target").mkdir()
        (remote / "not-a-target" / "readme.txt").write_text("x", encoding="utf-8")
        _git("init", "-b", "main", cwd=remote)
        _git("add", ".", cwd=remote)
        _git("commit", "-m", "init", cwd=remote)
        record = create_marketplace(db_session, "plain", f"file://{remote}")
        items = list_marketplace_plugins(db_session, record["id"])
        assert [i["name"] for i in items] == ["my-skill"]
        assert items[0]["target_type"] == "skill"

    def test_list_requires_synced_market(self, db_session, marketplace_base):
        # 未同步成功的市场（pending）不能浏览
        db_session.add(PluginMarketplace(name="pending", git_url="https://x.git", sync_status="pending"))
        db_session.commit()
        market = db_session.query(PluginMarketplace).filter_by(git_url="https://x.git").first()
        with pytest.raises(MarketplaceError, match="尚未同步"):
            list_marketplace_plugins(db_session, market.id)

    def test_list_unknown_market_rejected(self, db_session, marketplace_base):
        with pytest.raises(MarketplaceError, match="不存在"):
            list_marketplace_plugins(db_session, 999)


class TestDeleteMarketplace:
    """删除市场：记录与本地目录同时清理"""

    def test_delete_removes_record_and_dir(self, db_session, marketplace_base, remote_marketplace):
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        local = Path(record["local_path"])
        assert local.is_dir()
        removed = delete_marketplace(db_session, record["id"])
        assert removed["name"] == "m"
        assert not local.exists()
        assert list_marketplaces(db_session) == []


class TestResolveMarketPluginPath:
    """评测执行期解析：正常、越界、市场缺失"""

    def test_resolve_ok(self, db_session, marketplace_base, remote_marketplace):
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        info = prepare_market_plugin_for_eval(record["id"], "./public/anc-demo")
        assert (Path(info["path"]) / ".claude-plugin" / "plugin.json").is_file()

    def test_resolve_traversal_rejected(self, db_session, marketplace_base, remote_marketplace):
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        with pytest.raises(MarketplaceError, match="未找到插件目录"):
            prepare_market_plugin_for_eval(record["id"], "../../outside")

    def test_resolve_market_missing(self, db_session, marketplace_base):
        with pytest.raises(MarketplaceError, match="不存在"):
            prepare_market_plugin_for_eval(424242, "./public/anc-demo")


class TestBranchSelection:
    """按分支评测：远端分支列表、分支独立副本、已有副本复用、并发首次拉取"""

    @pytest.fixture
    def branched_market(self, db_session, marketplace_base, remote_marketplace):
        """main 与 feature 两个分支：feature 上新增一个 only-feature 插件"""
        _git("checkout", "-b", "feature", cwd=remote_marketplace)
        feature_plugin = remote_marketplace / "public" / "only-feature"
        (feature_plugin / ".claude-plugin").mkdir(parents=True)
        (feature_plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "only-feature"}), encoding="utf-8")
        manifest = remote_marketplace / ".claude-plugin" / "marketplace.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["plugins"].append(
            {"name": "only-feature", "description": "", "source": "./public/only-feature"}
        )
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _git("add", ".", cwd=remote_marketplace)
        _git("commit", "-m", "feature only", cwd=remote_marketplace)
        _git("checkout", "main", cwd=remote_marketplace)
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        return record

    def _branch_head(self, remote: Path, branch: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", "--short=12", branch], cwd=str(remote),
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def test_list_branches_default_first(self, db_session, branched_market):
        branches = svc.list_marketplace_branches(db_session, branched_market["id"])
        names = [b["name"] for b in branches]
        assert set(names) == {"main", "feature"}
        assert names[0] == "main"
        assert branches[0]["is_default"] is True
        assert branches[0]["cached"] is False

    def test_list_plugins_from_branch(self, db_session, branched_market, remote_marketplace):
        main_names = {i["name"] for i in list_marketplace_plugins(db_session, branched_market["id"])}
        feature_names = {
            i["name"] for i in list_marketplace_plugins(
                db_session, branched_market["id"], branch="feature")
        }
        assert "only-feature" not in main_names
        assert "only-feature" in feature_names
        assert "anc-demo" in feature_names

    def test_prepare_uses_branch_copy_and_commit(self, db_session, branched_market, remote_marketplace):
        info = prepare_market_plugin_for_eval(
            branched_market["id"], "./public/only-feature", branch="feature")
        expected_commit = self._branch_head(remote_marketplace, "feature")
        assert info["branch"] == "feature"
        assert info["branch_selected"] == "feature"
        assert info["commit"] == expected_commit
        assert info["repo_root"] != branched_market["local_path"]
        assert (Path(info["path"]) / ".claude-plugin" / "plugin.json").is_file()

    def test_default_branch_unchanged(self, db_session, branched_market):
        info = prepare_market_plugin_for_eval(branched_market["id"], "./public/anc-demo")
        assert info["branch_selected"] == ""
        assert info["repo_root"] == branched_market["local_path"]
        assert info["commit"] == svc._head_commit(Path(branched_market["local_path"]))

    def test_existing_branch_copy_reused_without_pull(
        self, db_session, branched_market, monkeypatch
    ):
        calls = []
        original = svc._clone_marketplace

        def counting(git_url, branch, dest):
            calls.append(branch)
            return original(git_url, branch, dest)

        monkeypatch.setattr(svc, "_clone_marketplace", counting)

        first = prepare_market_plugin_for_eval(
            branched_market["id"], "./public/anc-demo", branch="feature")
        second = prepare_market_plugin_for_eval(
            branched_market["id"], "./public/anc-demo", branch="feature")

        assert calls == ["feature"]  # 只拉一次，第二次直接复用
        assert first["path"] == second["path"]
        assert first["commit"] == second["commit"]

    def test_branch_names_isolated(self, db_session, branched_market):
        """不同分支各用独立目录：同一插件在两分支的路径不同"""
        main_info = prepare_market_plugin_for_eval(
            branched_market["id"], "./public/anc-demo")
        dev_info = prepare_market_plugin_for_eval(
            branched_market["id"], "./public/anc-demo", branch="feature")
        assert main_info["path"] != dev_info["path"]
        assert dev_info["path"].startswith(dev_info["repo_root"])
        assert main_info["path"].startswith(main_info["repo_root"])

    def test_invalid_branch_rejected(self, db_session, branched_market):
        for bad in ("-evil", "a b", "a..b", "x.lock", "a//b", "分支"):
            with pytest.raises(MarketplaceError, match="分支名不合法"):
                list_marketplace_plugins(db_session, branched_market["id"], branch=bad)

    def test_branch_not_on_remote_raises(self, db_session, branched_market):
        with pytest.raises(MarketplaceError, match="拉取失败"):
            prepare_market_plugin_for_eval(
                branched_market["id"], "./public/anc-demo", branch="no-such-branch")

    def test_concurrent_first_pull_clones_once(self, db_session, branched_market, monkeypatch):
        """并发首次拉同一分支：flock 串行化，只克隆一次，其余复用"""
        from concurrent.futures import ThreadPoolExecutor

        calls = []
        original = svc._clone_marketplace

        def counting(git_url, branch, dest):
            calls.append(branch)
            return original(git_url, branch, dest)

        monkeypatch.setattr(svc, "_clone_marketplace", counting)
        market = (
            db_session.query(PluginMarketplace)
            .filter(PluginMarketplace.id == branched_market["id"]).first()
        )

        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(lambda _: svc._ensure_branch_copy(market, "feature"), range(4)))

        assert calls == ["feature"]
        assert len(set(paths)) == 1
        assert (paths[0] / ".git").exists()

    def test_delete_cleans_branch_copies(self, db_session, branched_market):
        prepare_market_plugin_for_eval(
            branched_market["id"], "./public/anc-demo", branch="feature")
        branch_root = svc._branch_base_dir() / str(branched_market["id"])
        assert branch_root.is_dir()
        delete_marketplace(db_session, branched_market["id"])
        assert not branch_root.exists()


def _age_market(db_session, market_id: int, seconds: int) -> None:
    """把市场上次同步时间人为拨旧（节流窗口判定用）"""
    market = db_session.query(PluginMarketplace).filter(PluginMarketplace.id == market_id).first()
    market.last_synced_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    db_session.commit()


def _remote_commit(remote: Path) -> str:
    """在"远端"市场仓库加一个提交，返回新 commit SHA"""
    (remote / "public" / "anc-demo" / "REVISION.md").write_text("rev", encoding="utf-8")
    _git("add", ".", cwd=remote)
    _git("commit", "-m", "revision", cwd=remote)
    return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=str(remote),
                          check=True, capture_output=True, text=True).stdout.strip()


class TestLazySync:
    """评测期懒加载：节流窗口、远端更新生效、失败降级、版本锚点"""

    def test_fresh_market_skips_fetch(self, db_session, marketplace_base, remote_marketplace, monkeypatch):
        """刚同步过的市场在窗口内不再 fetch（避免连续评测重复拉仓库）"""
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        calls = []
        monkeypatch.setattr(svc, "_fetch_and_reset", lambda *a, **k: calls.append(a))

        info = prepare_market_plugin_for_eval(record["id"], "./public/anc-demo")

        assert calls == []
        assert info["refreshed"] is False
        assert info["stale"] is False

    def test_interval_boundary(self, db_session, marketplace_base, remote_marketplace, monkeypatch):
        """窗口内跳过、超出窗口才拉（默认窗口 60s）"""
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        calls = []
        monkeypatch.setattr(svc, "_fetch_and_reset", lambda *a, **k: calls.append(1))

        _age_market(db_session, record["id"], 30)
        prepare_market_plugin_for_eval(record["id"], "./public/anc-demo")
        assert calls == []

        _age_market(db_session, record["id"], 61)
        info = prepare_market_plugin_for_eval(record["id"], "./public/anc-demo")
        assert calls == [1]
        assert info["refreshed"] is True

    def test_min_interval_zero_always_pulls(self, db_session, marketplace_base, remote_marketplace, monkeypatch):
        """min_interval=0 → 每次评测都拉（严格最新口径）"""
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        calls = []
        monkeypatch.setattr(svc, "_fetch_and_reset", lambda *a, **k: calls.append(1))

        prepare_market_plugin_for_eval(record["id"], "./public/anc-demo", min_interval=0)
        assert calls == [1]

    def test_stale_market_pulls_remote_latest(self, db_session, marketplace_base, remote_marketplace):
        """窗口外评测：拉到远端最新提交，评测对象就是新版"""
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        before = prepare_market_plugin_for_eval(record["id"], "./public/anc-demo")

        new_commit = _remote_commit(remote_marketplace)
        _age_market(db_session, record["id"], 300)
        after = prepare_market_plugin_for_eval(record["id"], "./public/anc-demo")

        assert after["refreshed"] is True
        assert after["commit"] == new_commit != before["commit"]
        assert (Path(after["path"]) / "REVISION.md").is_file()

    def test_fetch_failure_degrades_to_local_copy(self, db_session, marketplace_base, remote_marketplace, monkeypatch):
        """拉取失败不拖垮评测：用本地副本继续，标 stale 留痕，且不动显式同步状态"""
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        _age_market(db_session, record["id"], 300)
        monkeypatch.setattr(svc, "_fetch_and_reset",
                            lambda *a, **k: (_ for _ in ()).throw(MarketplaceError("net down")))

        info = prepare_market_plugin_for_eval(record["id"], "./public/anc-demo")

        assert info["refreshed"] is False
        assert info["stale"] is True
        assert "net down" in info["refresh_error"]
        assert (Path(info["path"]) / ".claude-plugin" / "plugin.json").is_file()
        rows = {m["id"]: m for m in list_marketplaces(db_session)}
        assert rows[record["id"]]["sync_status"] == "ok"
        assert list_marketplace_plugins(db_session, record["id"])  # 浏览不受影响

    def test_missing_dir_reclones_even_within_window(self, db_session, marketplace_base, remote_marketplace):
        """本地目录丢失时无视节流窗口重新克隆"""
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        import shutil
        shutil.rmtree(record["local_path"])

        info = prepare_market_plugin_for_eval(record["id"], "./public/anc-demo")

        assert info["refreshed"] is True
        assert (Path(info["path"]) / ".claude-plugin" / "plugin.json").is_file()

    def test_missing_dir_and_clone_failure_raises(self, db_session, marketplace_base, remote_marketplace, monkeypatch):
        """本地副本缺失且重克隆失败：无副本可降级，按路径解析失败上抛"""
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        import shutil
        shutil.rmtree(record["local_path"])
        monkeypatch.setattr(svc, "_clone_marketplace",
                            lambda *a, **k: (_ for _ in ()).throw(MarketplaceError("clone down")))

        with pytest.raises(MarketplaceError, match="市场本地目录缺失"):
            prepare_market_plugin_for_eval(record["id"], "./public/anc-demo")

    def test_commit_anchor_matches_local_head(self, db_session, marketplace_base, remote_marketplace):
        """版本锚点 = 本地副本 HEAD 短 SHA（与远端提交一致）"""
        record = create_marketplace(db_session, "m", f"file://{remote_marketplace}")
        expected = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=record["local_path"],
                                  check=True, capture_output=True, text=True).stdout.strip()

        info = prepare_market_plugin_for_eval(record["id"], "./public/anc-demo")

        assert info["commit"] == expected
        assert info["market_id"] == record["id"]
        assert info["branch"] == ""

    def test_head_commit_outside_repo_returns_empty(self, tmp_path):
        assert svc._head_commit(tmp_path) == ""

    def test_seconds_since_naive_treated_as_utc(self):
        naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=100)
        assert 95 <= svc._seconds_since(naive) <= 105
        assert svc._seconds_since(None) is None

    def test_env_interval_override(self, monkeypatch):
        monkeypatch.setenv("PLUGIN_MARKET_LAZY_SYNC_INTERVAL", "5")
        assert svc._lazy_min_interval() == 5.0
        monkeypatch.setenv("PLUGIN_MARKET_LAZY_SYNC_INTERVAL", "abc")  # 非法值回落默认
        assert svc._lazy_min_interval() == float(svc.LAZY_SYNC_MIN_INTERVAL)
        monkeypatch.delenv("PLUGIN_MARKET_LAZY_SYNC_INTERVAL")
        assert svc._lazy_min_interval() == float(svc.LAZY_SYNC_MIN_INTERVAL)
