#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_patch.py (方案 A 完整实现)

说明：
- 使用基准包内的 version.json 的 "files" 字段作为权威 manifest（如果存在），
  仅对 manifest 中列出的资源进行比较与打包。
- 如果 manifest 不存在，则回退到按资源子路径比较（兼容旧逻辑）。
- 更稳健地查找本地 version.json，并在 res_version 为空时尝试回退策略。
- 不修改 workflow、无需额外运行参数，直接替换并运行即可。
"""

import os
import json
import hashlib
import shutil
import zipfile
import requests
import rarfile
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime

# ================= 配置常量 =================
MAIN_REPO_API = "https://api.github.com/repos/NiceAfternoon/arknights-mower/releases/latest"
PATCH_DIR = Path("patch")
SOURCE_RESOURCE_DIR = Path("resource")
# 尝试多种可能的 version.json 路径（按优先级）
POSSIBLE_VERSION_PATHS = [
    SOURCE_RESOURCE_DIR / "arknights_mower" / "data" / "version.json",
    SOURCE_RESOURCE_DIR / "data" / "version.json",
    Path("arknights_mower") / "data" / "version.json",
    SOURCE_RESOURCE_DIR / "version.json",
]
# 只把这些资源子路径作为“资源”（当 manifest 不存在时使用）
RESOURCE_ROOT_SUBPATHS = [
    "arknights_mower/data",
    "arknights_mower/fonts",
    "arknights_mower/models",
    "arknights_mower/opname",
    "ui"
]

LOG_FILE = Path("patch_run.log")

# ================ 工具函数 ================
def safe_mkdir(p: Path):
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def append_log(*args, **kwargs):
    print(*args, **kwargs)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            print(*args, **kwargs, file=f)
    except Exception:
        pass

def md5_of_file(filepath: Path) -> str:
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()

def is_under_resource_subpath(rel_path: str) -> bool:
    """判断相对路径是否属于我们关心的资源子路径"""
    for sub in RESOURCE_ROOT_SUBPATHS:
        if rel_path == sub or rel_path.startswith(sub + "/"):
            return True
    return False

# ================ 主类 ================
class PatchGenerator:
    def __init__(self):
        safe_mkdir(PATCH_DIR)
        self.tmp_dir = Path(".tmp_patch_build")
        safe_mkdir(self.tmp_dir)

        self.session = self._build_retry_session()
        self.latest_app_tag = self._get_latest_release_tag()
        self.target_res_version = self._get_target_res_version()
        self.res_tag_short = self.target_res_version.replace(".", "")
        append_log(f"DEBUG: latest_app_tag={self.latest_app_tag}, target_res_version={self.target_res_version}")

    def _build_retry_session(self):
        session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            session.headers.update({"Authorization": f"Bearer {token}"})
        return session

    def _get_latest_release_tag(self):
        try:
            resp = self.session.get(MAIN_REPO_API, timeout=10)
            resp.raise_for_status()
            return resp.json().get("tag_name", "v0.0.0")
        except Exception as e:
            append_log(f"获取 App Tag 失败: {e}")
            return "v0.0.0"

    def _get_target_res_version(self):
        """
        尝试多种路径读取本地 version.json。
        如果 res_version 为空，尝试使用 last_updated 或 files 指纹作为回退。
        """
        for p in POSSIBLE_VERSION_PATHS:
            try:
                if p.exists():
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        ver = (data.get("res_version") or data.get("version") or "").strip()
                        if ver:
                            append_log(f"DEBUG: 使用 version.json: {p} -> res_version={ver}")
                            return ver
                        # 回退：last_updated
                        last = (data.get("last_updated") or data.get("updated_at") or "").strip()
                        if last:
                            short = "".join(ch for ch in last if ch.isdigit())
                            if short:
                                append_log(f"DEBUG: res_version 为空，使用 last_updated 回退 -> {short}")
                                return short
                        # 回退：files 指纹
                        files = data.get("files")
                        if isinstance(files, dict) and files:
                            sample = "".join(f"{k}:{v}" for i,(k,v) in enumerate(sorted(files.items())) if i < 50)
                            h = hashlib.md5(sample.encode("utf-8")).hexdigest()
                            fallback = h[:12]
                            append_log(f"DEBUG: res_version 为空，使用 files 指纹回退 -> {fallback}")
                            return fallback
            except Exception as e:
                append_log(f"DEBUG: 读取 {p} 失败: {e}")
        append_log("DEBUG: 未找到 version.json，使用默认 0000.0000.0000")
        return "0000.0000.0000"

    def _get_file_tree(self, root_dir: Path) -> dict:
        """
        只返回资源子路径下的文件树（相对路径以 search_root 为基准）。
        search_root:
          - 如果 root_dir 下有 resource/ -> 使用 root_dir/resource 作为 search_root（开发仓库）
          - 否则尝试 root_dir/_internal 或 root_dir/*/_internal（Release 解压）
        只记录属于 RESOURCE_ROOT_SUBPATHS 的文件（当 manifest 不存在时作为后备）。
        """
        tree = {}

        # 选择 search_root
        if (root_dir / "resource").exists():
            search_root = root_dir / "resource"
            append_log(f"DEBUG: 使用本地资源仓库 search_root={search_root}")
        else:
            if (root_dir / "_internal").exists():
                search_root = root_dir / "_internal"
                append_log(f"DEBUG: 直接找到 _internal: {search_root}")
            else:
                found = None
                for sub in root_dir.iterdir():
                    if sub.is_dir() and (sub / "_internal").exists():
                        found = sub / "_internal"
                        break
                if found:
                    search_root = found
                    append_log(f"DEBUG: 在子目录中找到 _internal: {search_root}")
                else:
                    append_log(f"DEBUG: 未找到 _internal 或 resource 在 {root_dir}")
                    return {}

        # 遍历 search_root，记录仅属于资源子路径的文件
        for p in search_root.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(search_root).as_posix()
            except Exception:
                continue
            # 只记录属于 RESOURCE_ROOT_SUBPATHS 的文件（后备策略）
            if is_under_resource_subpath(rel):
                try:
                    tree[rel] = md5_of_file(p)
                except Exception as e:
                    append_log(f"警告: 计算 MD5 失败 {p}: {e}")
        return tree

    def _read_manifest_from_base(self, base_dir: Path):
        """
        尝试从基准包的常见位置读取 version.json 并返回 manifest_files set（keys of files dict）。
        返回 None 表示未找到 manifest。
        """
        candidates = [
            Path(base_dir) / "_internal" / "arknights_mower" / "data" / "version.json",
            Path(base_dir) / "_internal" / "resource" / "arknights_mower" / "data" / "version.json",
            Path(base_dir) / "arknights_mower" / "data" / "version.json",
            Path(base_dir) / "resource" / "arknights_mower" / "data" / "version.json",
        ]
        for mpath in candidates:
            try:
                if mpath.exists():
                    with open(mpath, "r", encoding="utf-8") as mf:
                        mj = json.load(mf)
                        files = mj.get("files")
                        if isinstance(files, dict) and files:
                            # keys are relative paths like "arknights_mower/data/xxx"
                            manifest = set(files.keys())
                            append_log(f"DEBUG: 从基准包读取 manifest: {mpath} (entries={len(manifest)})")
                            return manifest
            except Exception as e:
                append_log(f"DEBUG: 读取基准包 manifest 失败 {mpath}: {e}")
        append_log("DEBUG: 基准包中未找到 manifest (version.json.files)")
        return None

    def generate_patch(self, base_tag: str):
        try:
            base_dir = self._download_and_extract_base(base_tag)
            if not base_dir:
                append_log(f"基准包 {base_tag} 获取失败，跳过")
                return

            # 尝试读取基准包 manifest（权威文件列表）
            manifest_files = self._read_manifest_from_base(base_dir)

            # 生成两棵树（按资源子路径的后备策略）
            old_tree = self._get_file_tree(base_dir)
            new_tree = self._get_file_tree(Path("."))

            append_log(f"DEBUG: RAW OLD_TREE count={len(old_tree)} RAW NEW_TREE count={len(new_tree)}")

            # 如果找到了 manifest，就用 manifest 过滤两棵树（只比较 manifest 中列出的文件）
            if manifest_files is not None:
                # 规范化：manifest keys 可能包含 leading "./" 或者不同分隔，确保一致性
                normalized_manifest = set()
                for k in manifest_files:
                    nk = k.replace("\\", "/").lstrip("./")
                    normalized_manifest.add(nk)
                manifest_files = normalized_manifest

                old_tree = {k: v for k, v in old_tree.items() if k in manifest_files}
                new_tree = {k: v for k, v in new_tree.items() if k in manifest_files}
                append_log(f"DEBUG: 使用 manifest 过滤后 OLD_TREE count={len(old_tree)} NEW_TREE count={len(new_tree)}")
            else:
                append_log("DEBUG: 未使用 manifest，继续使用资源子路径比较（后备策略）")

            # 计算差分（基于 manifest 或资源子路径）
            added = [f for f in new_tree if f not in old_tree]
            modified = [f for f in new_tree if f in old_tree and new_tree[f] != old_tree[f]]
            # removed 只包含 manifest 中存在但本地缺失的文件（如果 manifest 存在）
            if manifest_files is not None:
                removed = [f for f in old_tree if f not in new_tree]
            else:
                # 如果没有 manifest，保留原来的 removed 逻辑（但这些可能是噪声）
                removed = [f for f in old_tree if f not in new_tree]

            append_log(f"DEBUG: added={len(added)} modified={len(modified)} removed={len(removed)}")

            if not (added or modified or removed):
                append_log(f"版本 {base_tag} 无变更，跳过生成")
                return

            base_label = base_tag.replace(".", "") if len(base_tag) >= 12 else base_tag[:8]
            file_name_base = f"from-{base_label}-to-{self.res_tag_short}-{self.latest_app_tag}"
            zip_name = f"{file_name_base}.zip"
            json_name = f"{file_name_base}.json"

            tmp_zip = PATCH_DIR / f"{zip_name}.tmp"
            tmp_json = PATCH_DIR / f"{json_name}.tmp"

            # 打包新增与修改的文件，路径基于 SOURCE_RESOURCE_DIR（优先）或当前目录（兼容）
            with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as z:
                for rel_path in added + modified:
                    # 额外防护：如果 manifest 存在，确保 rel_path 在 manifest 中；否则确保属于资源子路径
                    if manifest_files is not None:
                        if rel_path not in manifest_files:
                            append_log(f"跳过非 manifest 路径: {rel_path}")
                            continue
                    else:
                        if not is_under_resource_subpath(rel_path):
                            append_log(f"跳过非资源路径: {rel_path}")
                            continue

                    source_file = SOURCE_RESOURCE_DIR / rel_path
                    if not source_file.exists():
                        alt_source = Path(".") / rel_path
                        if alt_source.exists():
                            source_file = alt_source
                        else:
                            append_log(f"警告: 源文件不存在，跳过 {rel_path}")
                            continue

                    try:
                        z.write(source_file, rel_path)
                    except Exception as e:
                        append_log(f"警告: 写入 zip 失败 {source_file}: {e}")

            meta = {
                "base_version": base_tag,
                "target_resource_full": self.target_res_version,
                "target_resource_short": self.res_tag_short,
                "software_tag": self.latest_app_tag,
                "files_added": added,
                "files_modified": modified,
                "files_removed": removed,
                "md5": md5_of_file(tmp_zip),
                "size": tmp_zip.stat().st_size,
                "generated_at": datetime.utcnow().isoformat()
            }

            with open(tmp_json, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

            os.replace(tmp_zip, PATCH_DIR / zip_name)
            os.replace(tmp_json, PATCH_DIR / json_name)
            append_log(f"--- 成功生成补丁包: {zip_name} ---")

        except Exception as e:
            append_log(f"生成补丁失败 [{base_tag}]: {e}")

    def _download_and_extract_base(self, tag_name: str) -> Path:
        extract_path = self.tmp_dir / tag_name
        if extract_path.exists():
            return extract_path

        append_log(f"正在获取基准包资源: {tag_name}")
        api_url = MAIN_REPO_API if tag_name == self.latest_app_tag else f"https://api.github.com/repos/NiceAfternoon/arknights-mower/releases/tags/{tag_name}"

        try:
            resp = self.session.get(api_url, timeout=10)
        except Exception as e:
            append_log(f"请求 Release API 失败: {e}")
            return None

        if resp.status_code != 200:
            append_log(f"无法找到基准版本 {tag_name} 的 Release (status={resp.status_code})")
            return None

        assets = resp.json().get("assets", [])
        dl_url, ext = None, ""
        for asset in assets:
            name = asset.get("name", "")
            if (tag_name in name or "mower" in name.lower()) and (name.endswith(".zip") or name.endswith(".rar")):
                dl_url = asset.get("browser_download_url")
                ext = Path(name).suffix
                break

        if not dl_url:
            append_log("未找到合适的压缩包资源")
            return None

        archive_path = self.tmp_dir / f"{tag_name}{ext}"
        try:
            with self.session.get(dl_url, stream=True) as r, open(archive_path, "wb") as f:
                shutil.copyfileobj(r.raw, f)
        except Exception as e:
            append_log(f"下载基准包失败: {e}")
            return None

        try:
            if ext == ".zip":
                with zipfile.ZipFile(archive_path, 'r') as z:
                    z.extractall(extract_path)
            else:
                with rarfile.RarFile(archive_path) as rf:
                    rf.extractall(extract_path)
        except Exception as e:
            append_log(f"解压基准包失败: {e}")
            return None
        finally:
            try:
                archive_path.unlink()
            except Exception:
                pass

        # 打印解压顶层与 _internal 第一层，便于 CI 日志查看
        try:
            append_log("DEBUG: extract_path:", extract_path)
            append_log("DEBUG: top-level entries:")
            for p in sorted(extract_path.iterdir()):
                append_log("  ", p.name, "(dir)" if p.is_dir() else "(file)")
            if (extract_path / "_internal").exists():
                append_log("DEBUG: _internal first layer:")
                for p in sorted((extract_path / "_internal").iterdir()):
                    append_log("  ", p.name, "(dir)" if p.is_dir() else "(file)")
            else:
                for sub in sorted(extract_path.iterdir()):
                    if sub.is_dir():
                        append_log("DEBUG: sub", sub.name, "has _internal:", (sub / "_internal").exists())
        except Exception:
            pass

        return extract_path

    def _cleanup_old_patches(self):
        for item in PATCH_DIR.glob("from-*-to-*.zip"):
            try:
                if (self.latest_app_tag not in item.name) or (self.res_tag_short not in item.name):
                    append_log(f"清理过期补丁: {item.name}")
                    item.unlink()
                    json_file = item.with_suffix('.json')
                    if json_file.exists():
                        json_file.unlink()
            except Exception as e:
                append_log(f"清理补丁失败 {item}: {e}")

    def run(self):
        old_res_tags = set()
        for meta_file in PATCH_DIR.glob("*.json"):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    m = json.load(f)
                    if m.get("software_tag") == self.latest_app_tag:
                        old_res_tags.add(m.get("target_resource_short"))
            except Exception:
                pass

        self._cleanup_old_patches()

        base_versions = {self.latest_app_tag}
        for tag in old_res_tags:
            if tag and tag != self.res_tag_short:
                base_versions.add(tag)

        for base in base_versions:
            self.generate_patch(base)

        try:
            if self.tmp_dir.exists():
                shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass

# ================ 脚本入口 ================
if __name__ == "__main__":
    try:
        if LOG_FILE.exists():
            LOG_FILE.unlink()
    except Exception:
        pass

    append_log("=== PatchGenerator start ===")
    pg = PatchGenerator()
    pg.run()
    append_log("=== PatchGenerator end ===")
