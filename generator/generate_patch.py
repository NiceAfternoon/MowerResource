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

# 配置常量
MAIN_REPO_API = "https://api.github.com/repos/ArkMowers/arknights-mower/releases/latest"
PATCH_DIR = Path("patch")
DATA_VERSION_FILE = Path("resource/arknights_mower/data/version.json")
# 定义需要监控的资源子目录
RESOURCE_SUBDIRS = ["arknights_mower", "ui"]

class PatchGenerator:
    def __init__(self):
        self.session = self._build_retry_session()
        self.latest_app_tag = self._get_latest_release_tag()
        self.target_res_version = self._get_target_res_version()
        self.res_tag_short = self.target_res_version[:8]
        self.tmp_dir = Path(".tmp_patch_build")
        
        PATCH_DIR.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def _build_retry_session(self):
        session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        if token := os.environ.get("GITHUB_TOKEN"):
            session.headers.update({"Authorization": f"Bearer {token}"})
        return session

    def _get_latest_release_tag(self):
        resp = self.session.get(MAIN_REPO_API, timeout=10)
        resp.raise_for_status()
        return resp.json().get("tag_name")

    def _get_target_res_version(self):
        with open(DATA_VERSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("last_updated", "unknown_res")

    def _md5(self, filepath: Path) -> str:
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def _get_file_tree(self, root_dir: Path) -> dict:
        """仅扫描指定的资源子目录，并返回相对于 resource 的路径"""
        tree = {}
        for subdir in RESOURCE_SUBDIRS:
            target_path = root_dir / "resource" / subdir
            if not target_path.exists():
                continue
            for filepath in target_path.rglob("*"):
                if filepath.is_file():
                    # 统一转为相对于 resource 的路径，例如 arknights_mower/data/v.json
                    rel_to_res = filepath.relative_to(root_dir / "resource").as_posix()
                    tree[rel_to_res] = self._md5(filepath)
        return tree

    def generate_patch(self, base_tag: str):
        tmp_zip, tmp_json = Path(), Path()
        try:
            base_dir = self._download_and_extract_base(base_tag)
            if not base_dir: return

            # 获取 Base 和当前环境的树（Key 已经是剥离 resource 后的路径）
            old_tree = self._get_file_tree(base_dir)
            new_tree = self._get_file_tree(Path("."))

            added = [f for f in new_tree if f not in old_tree]
            modified = [f for f in new_tree if f in old_tree and new_tree[f] != old_tree[f]]
            removed = [f for f in old_tree if f not in new_tree]

            if not (added or modified or removed): 
                print(f"版本 {base_tag} 无需更新")
                return

            file_name_base = f"from-{base_tag}-to-{self.res_tag_short}-{self.latest_app_tag}"
            zip_name = f"{file_name_base}.zip"
            json_name = f"{file_name_base}.json"
            
            tmp_zip = PATCH_DIR / f"{zip_name}.tmp"
            tmp_json = PATCH_DIR / f"{json_name}.tmp"

            with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as z:
                for rel_path in added + modified:
                    # rel_path 已经是 arknights_mower/... 结构
                    source_file = Path("resource") / rel_path
                    z.write(source_file, rel_path)

            meta = {
                "base_version": base_tag,
                "target_resource_full": self.target_res_version,
                "target_resource_short": self.res_tag_short,
                "software_tag": self.latest_app_tag,
                "files_added": added,
                "files_modified": modified,
                "files_removed": removed,
                "md5": self._md5(tmp_zip),
                "size": tmp_zip.stat().st_size,
                "generated_at": datetime.utcnow().isoformat()
            }
            with open(tmp_json, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

            os.replace(tmp_zip, PATCH_DIR / zip_name)
            os.replace(tmp_json, PATCH_DIR / json_name)
            print(f"成功生成增量包: {zip_name}")

        except Exception as e:
            print(f"生成补丁失败 {base_tag}: {e}")
            if tmp_zip.exists(): tmp_zip.unlink()

    def _download_and_extract_base(self, tag_name: str) -> Path:
        extract_path = self.tmp_dir / tag_name
        if extract_path.exists(): return extract_path

        print(f"下载基准包: {tag_name}")
        api_url = f"https://api.github.com/repos/ArkMowers/arknights-mower/releases/tags/{tag_name}"
        if tag_name == self.latest_app_tag:
            api_url = MAIN_REPO_API

        resp = self.session.get(api_url, timeout=10)
        if resp.status_code != 200: return None
        
        assets = resp.json().get("assets", [])
        dl_url, ext = None, ""
        for asset in assets:
            name = asset["name"]
            if tag_name in name and (name.endswith(".zip") or name.endswith(".rar")):
                dl_url = asset["browser_download_url"]
                ext = Path(name).suffix
                break
        
        if not dl_url: return None
        archive_path = self.tmp_dir / f"{tag_name}{ext}"
        with self.session.get(dl_url, stream=True) as r, open(archive_path, "wb") as f:
            shutil.copyfileobj(r.raw, f)

        if ext == ".zip":
            with zipfile.ZipFile(archive_path, 'r') as z: z.extractall(extract_path)
        else:
            with rarfile.RarFile(archive_path) as rf: rf.extractall(extract_path)
        
        archive_path.unlink()
        return extract_path

    def _cleanup_old_patches(self):
        for item in PATCH_DIR.glob("from-*-to-*.zip"):
            if (self.latest_app_tag not in item.name) or (self.res_tag_short not in item.name):
                item.unlink()
                json_file = item.with_suffix('.json')
                if json_file.exists(): json_file.unlink()

    def run(self):
        old_res_tags = set()
        for meta_file in PATCH_DIR.glob("*.json"):
            try:
                with open(meta_file, "r") as f:
                    m = json.load(f)
                    if m.get("software_tag") == self.latest_app_tag:
                        old_res_tags.add(m.get("target_resource_short"))
            except: pass

        self._cleanup_old_patches()
        base_versions = {self.latest_app_tag}
        for tag in old_res_tags:
            if tag and tag != self.res_tag_short:
                base_versions.add(tag)

        for base in base_versions:
            self.generate_patch(base)

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    PatchGenerator().run()