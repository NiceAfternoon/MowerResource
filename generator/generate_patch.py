#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_patch.py - manifest-only comparison mode

Behavior:
- Prefer manifest-based comparison: read base manifest (release version.json files)
  and local manifest (resource/.../version.json files). Compare keys and hashes.
- If local manifest lacks 'files', compute local files' md5 for the manifest entries.
- Only package added and modified files.
- No directory-wide scanning required when manifests are present.
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
# 本地可能的 version.json 路径（按优先级）
LOCAL_VERSION_CANDIDATES = [
    SOURCE_RESOURCE_DIR / "arknights_mower" / "data" / "version.json",
    SOURCE_RESOURCE_DIR / "data" / "version.json",
    SOURCE_RESOURCE_DIR / "version.json",
    Path("arknights_mower") / "data" / "version.json",
]
LOG_FILE = Path("patch_run.log")

# ================ 工具函数 ================
def append_log(*args, **kwargs):
    print(*args, **kwargs)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            print(*args, **kwargs, file=f)
    except Exception:
        pass

def md5_of_file(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()

def normalize_key(k: str) -> str:
    return k.replace("\\", "/").lstrip("./")

# ================ 主类 ================
class PatchGenerator:
    def __init__(self):
        PATCH_DIR.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = Path(".tmp_patch_build")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.session = self._build_retry_session()
        self.latest_app_tag = self._get_latest_release_tag()
        self.target_res_version = self._get_local_res_version_fallback()
        self.res_tag_short = self.target_res_version.replace(".", "")
        append_log(f"DEBUG: latest_app_tag={self.latest_app_tag}, target_res_version={self.target_res_version}")

    def _build_retry_session(self):
        s = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429,500,502,503,504])
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            s.headers.update({"Authorization": f"Bearer {token}"})
        return s

    def _get_latest_release_tag(self):
        try:
            r = self.session.get(MAIN_REPO_API, timeout=10)
            r.raise_for_status()
            return r.json().get("tag_name", "v0.0.0")
        except Exception as e:
            append_log("WARN: 获取最新 release tag 失败:", e)
            return "v0.0.0"

    def _get_local_res_version_fallback(self):
        # 读取本地 version.json 的 res_version 或回退到 empty
        for p in LOCAL_VERSION_CANDIDATES:
            try:
                if p.exists():
                    with open(p, "r", encoding="utf-8") as f:
                        j = json.load(f)
                        rv = (j.get("res_version") or j.get("version") or "").strip()
                        if rv:
                            append_log(f"DEBUG: 本地 version.json: {p} -> res_version={rv}")
                            return rv
                        # fallback: last_updated or files fingerprint
                        last = (j.get("last_updated") or "").strip()
                        if last:
                            short = "".join(ch for ch in last if ch.isdigit())
                            if short:
                                append_log(f"DEBUG: 本地 res_version 为空，使用 last_updated 回退 -> {short}")
                                return short
                        files = j.get("files")
                        if isinstance(files, dict) and files:
                            sample = "".join(f"{k}:{v}" for i,(k,v) in enumerate(sorted(files.items())) if i < 50)
                            h = hashlib.md5(sample.encode("utf-8")).hexdigest()[:12]
                            append_log(f"DEBUG: 本地 res_version 为空，使用 files 指纹回退 -> {h}")
                            return h
            except Exception as e:
                append_log("DEBUG: 读取本地 version.json 失败:", p, e)
        append_log("DEBUG: 未找到本地 version.json，使用默认 0000.0000.0000")
        return "0000.0000.0000"

    def _download_and_extract_base(self, tag_name: str) -> Path:
        extract_path = self.tmp_dir / tag_name
        if extract_path.exists():
            return extract_path

        append_log(f"正在获取基准包资源: {tag_name}")
        api_url = MAIN_REPO_API if tag_name == self.latest_app_tag else f"https://api.github.com/repos/NiceAfternoon/arknights-mower/releases/tags/{tag_name}"
        try:
            resp = self.session.get(api_url, timeout=10)
        except Exception as e:
            append_log("WARN: 请求 Release API 失败:", e)
            return None
        if resp.status_code != 200:
            append_log("WARN: Release 未找到, status:", resp.status_code)
            return None

        assets = resp.json().get("assets", [])
        dl_url, ext = None, ""
        for a in assets:
            name = a.get("name","")
            if (tag_name in name or "mower" in name.lower()) and (name.endswith(".zip") or name.endswith(".rar")):
                dl_url = a.get("browser_download_url")
                ext = Path(name).suffix
                break
        if not dl_url:
            append_log("WARN: 未找到合适的压缩包资源")
            return None

        archive_path = self.tmp_dir / f"{tag_name}{ext}"
        try:
            with self.session.get(dl_url, stream=True) as r, open(archive_path, "wb") as f:
                shutil.copyfileobj(r.raw, f)
        except Exception as e:
            append_log("WARN: 下载基准包失败:", e)
            return None

        try:
            if ext == ".zip":
                with zipfile.ZipFile(archive_path, 'r') as z:
                    z.extractall(extract_path)
            else:
                with rarfile.RarFile(archive_path) as rf:
                    rf.extractall(extract_path)
        except Exception as e:
            append_log("WARN: 解压失败:", e)
            return None
        finally:
            try:
                archive_path.unlink()
            except Exception:
                pass

        append_log("DEBUG: extract_path:", extract_path)
        return extract_path

    def _read_manifest_from_base(self, base_dir: Path):
        # 尝试常见位置读取基准包 version.json 的 files 字段
        candidates = [
            base_dir / "_internal" / "arknights_mower" / "data" / "version.json",
            base_dir / "_internal" / "resource" / "arknights_mower" / "data" / "version.json",
            base_dir / "arknights_mower" / "data" / "version.json",
            base_dir / "resource" / "arknights_mower" / "data" / "version.json",
        ]
        for p in candidates:
            try:
                if p.exists():
                    with open(p, "r", encoding="utf-8") as f:
                        j = json.load(f)
                        files = j.get("files")
                        if isinstance(files, dict) and files:
                            manifest = { normalize_key(k): v for k,v in files.items() }
                            append_log(f"DEBUG: 从基准包读取 manifest: {p} (entries={len(manifest)})")
                            return manifest
            except Exception as e:
                append_log("DEBUG: 读取基准包 manifest 失败:", p, e)
        append_log("DEBUG: 基准包中未找到 manifest")
        return None

    def _read_local_manifest_or_build(self):
        # 读取本地 version.json files，若不存在则尝试从 resource/ 中构建（仅对 manifest keys 需要的文件计算 md5）
        for p in LOCAL_VERSION_CANDIDATES:
            try:
                if p.exists():
                    with open(p, "r", encoding="utf-8") as f:
                        j = json.load(f)
                        files = j.get("files")
                        if isinstance(files, dict) and files:
                            local = { normalize_key(k): v for k,v in files.items() }
                            append_log(f"DEBUG: 读取本地 manifest: {p} (entries={len(local)})")
                            return local
                        # if files empty, still return j for fallback
                        return {"__raw__": j, "__path__": str(p)}
            except Exception as e:
                append_log("DEBUG: 读取本地 version.json 失败:", p, e)
        # 没有本地 version.json，尝试从 resource/ 构建 minimal manifest by scanning known resource dirs
        append_log("DEBUG: 本地未找到 manifest，尝试从 resource/ 构建（仅常见资源目录）")
        built = {}
        # common resource roots
        roots = [
            SOURCE_RESOURCE_DIR / "arknights_mower" / "data",
            SOURCE_RESOURCE_DIR / "arknights_mower" / "fonts",
            SOURCE_RESOURCE_DIR / "arknights_mower" / "models",
            SOURCE_RESOURCE_DIR / "arknights_mower" / "opname",
            SOURCE_RESOURCE_DIR / "ui",
        ]
        for r in roots:
            if not r.exists():
                continue
            for f in r.rglob("*"):
                if f.is_file():
                    rel = normalize_key(str(f.relative_to(SOURCE_RESOURCE_DIR)))
                    try:
                        built[rel] = md5_of_file(f)
                    except Exception:
                        pass
        append_log(f"DEBUG: 从 resource 构建本地 manifest entries={len(built)}")
        return built

    def generate_patch(self, base_tag: str):
        base_dir = self._download_and_extract_base(base_tag)
        if not base_dir:
            append_log("WARN: 无法获取基准包，跳过")
            return

        base_manifest = self._read_manifest_from_base(base_dir)
        local_manifest_obj = self._read_local_manifest_or_build()

        # If local_manifest_obj is a wrapper with raw json, try to extract files
        if isinstance(local_manifest_obj, dict) and "__raw__" in local_manifest_obj:
            raw = local_manifest_obj["__raw__"]
            files = raw.get("files")
            if isinstance(files, dict) and files:
                local_manifest = { normalize_key(k): v for k,v in files.items() }
            else:
                # fallback: empty dict
                local_manifest = {}
        else:
            local_manifest = local_manifest_obj or {}

        append_log(f"DEBUG: base_manifest entries={len(base_manifest) if base_manifest else 0} local_manifest entries={len(local_manifest)}")

        # If base manifest exists, use it as authoritative list of keys to compare
        if base_manifest is not None:
            keys = set(base_manifest.keys()) | set(local_manifest.keys())
            added = []
            modified = []
            removed = []
            for k in sorted(keys):
                base_hash = base_manifest.get(k)
                local_hash = local_manifest.get(k)
                if local_hash is None:
                    # local missing
                    if base_hash is not None:
                        removed.append(k)
                    else:
                        # neither? skip
                        pass
                else:
                    if base_hash is None:
                        # present locally but not in base manifest -> added
                        added.append(k)
                    else:
                        # both present: compare hashes
                        if local_hash != base_hash:
                            modified.append(k)
            append_log(f"DEBUG(manifest): added={len(added)} modified={len(modified)} removed={len(removed)}")
        else:
            # fallback: compare local_manifest keys only (we built local manifest by scanning)
            keys = set(local_manifest.keys())
            added = list(keys)
            modified = []
            removed = []
            append_log(f"DEBUG(fallback): treating all local files as added count={len(added)}")

        # If local manifest entries are hashes computed from resource files, ensure we have actual files to package
        # Prepare zip
        if not (added or modified or removed):
            append_log("版本无变更，跳过生成")
            return

        base_label = base_tag.replace(".", "") if len(base_tag) >= 12 else base_tag[:8]
        file_name_base = f"from-{base_label}-to-{self.res_tag_short}-{self.latest_app_tag}"
        zip_name = f"{file_name_base}.zip"
        json_name = f"{file_name_base}.json"
        tmp_zip = PATCH_DIR / f"{zip_name}.tmp"
        tmp_json = PATCH_DIR / f"{json_name}.tmp"

        with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as z:
            for rel in added + modified:
                # rel is normalized path like "arknights_mower/data/xxx.json" or "ui/..."
                # Try candidate source locations
                candidates = [
                    SOURCE_RESOURCE_DIR / rel,
                    Path(".") / rel,
                    SOURCE_RESOURCE_DIR / Path(rel).relative_to("arknights_mower") if rel.startswith("arknights_mower/") else None
                ]
                found = None
                for c in candidates:
                    if c and c.exists():
                        found = c
                        break
                if not found:
                    append_log(f"警告: 源文件未找到，跳过 {rel} (tried {candidates})")
                    continue
                try:
                    z.write(found, rel)
                    append_log(f"DEBUG: 打包 {rel} <- {found}")
                except Exception as e:
                    append_log(f"警告: 写入 zip 失败 {found}: {e}")

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

    def _cleanup_old_patches(self):
        for item in PATCH_DIR.glob("from-*-to-*.zip"):
            try:
                if (self.latest_app_tag not in item.name) or (self.res_tag_short not in item.name):
                    append_log("清理过期补丁:", item.name)
                    item.unlink()
                    jf = item.with_suffix('.json')
                    if jf.exists():
                        jf.unlink()
            except Exception as e:
                append_log("清理补丁失败:", e)

    def run(self):
        # collect base versions to compare (existing patch jsons)
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

        base_versions = { self.latest_app_tag }
        for t in old_res_tags:
            if t and t != self.res_tag_short:
                base_versions.add(t)

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
