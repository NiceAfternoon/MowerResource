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

class PatchGenerator:
    def __init__(self):
        self.session = self._build_retry_session()
        self.latest_app_tag = self._get_latest_release_tag()
        self.target_res_version = self._get_target_res_version()
        # 取 last_updated 的前 8 位作为文件标识 (例如: 26-03-31)
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

    def _cleanup_old_patches(self):
        """
        清理逻辑：
        1. 如果文件名不含当前最新的软件版本 Tag (latest_app_tag)，说明软件已更新，删除。
        2. 如果包含软件 Tag 但资源标识 (res_tag_short) 不是最新的，说明资源已更新，删除。
        """
        print(f"清理中... 软件版本: {self.latest_app_tag}, 资源标识: {self.res_tag_short}")
        
        for item in PATCH_DIR.glob("from-*-to-*.zip"):
            # 必须同时包含最新的软件 Tag 和 最新的资源短 Tag
            if (self.latest_app_tag not in item.name) or (self.res_tag_short not in item.name):
                print(f"清理过期包: {item.name}")
                item.unlink()
                json_file = item.with_suffix('.json')
                if json_file.exists(): json_file.unlink()

    def _download_and_extract_base(self, tag_name: str) -> Path:
        extract_path = self.tmp_dir / tag_name
        if extract_path.exists():
            return extract_path

        print(f"下载基准全量包: {tag_name}")
        api_url = f"https://api.github.com/repos/ArkMowers/arknights-mower/releases/tags/{tag_name}"
        if tag_name == self.latest_app_tag:
            api_url = MAIN_REPO_API

        resp = self.session.get(api_url, timeout=10)
        if resp.status_code != 200:
            return None

        data = resp.json()
        real_tag = data.get("tag_name")
        assets = data.get("assets", [])
        
        dl_url, ext = None, ""
        for asset in assets:
            name = asset["name"]
            if real_tag in name and (name.endswith(".rar") or name.endswith(".zip")):
                dl_url = asset["browser_download_url"]
                ext = ".rar" if name.endswith(".rar") else ".zip"
                break
        
        if not dl_url: return None

        archive_path = self.tmp_dir / f"{real_tag}{ext}"
        with self.session.get(dl_url, stream=True) as r, open(archive_path, "wb") as f:
            shutil.copyfileobj(r.raw, f)

        if ext == ".zip":
            with zipfile.ZipFile(archive_path, 'r') as z: z.extractall(extract_path)
        else:
            with rarfile.RarFile(archive_path) as rf: rf.extractall(extract_path)

        archive_path.unlink()
        return extract_path

    def _get_file_tree(self, directory: Path) -> dict:
        tree = {}
        exclude_dirs = {'.git', 'patch', '.tmp_patch_build', '__pycache__'}
        for filepath in directory.rglob("*"):
            if any(part in filepath.parts for part in exclude_dirs): continue
            if filepath.is_file():
                rel_path = filepath.relative_to(directory).as_posix()
                tree[rel_path] = self._md5(filepath)
        return tree

    def generate_patch(self, base_tag: str, is_res_base=False):
        """
        base_tag: 可以是软件版本 (v4.1.2) 或 资源版本的前8位 (26-03-31)
        """
        tmp_zip, tmp_json = Path(), Path()
        try:
            # 如果基准是软件版本，尝试下载；如果是资源版本，需要从本地找之前的缓存或跳过
            base_dir = self._download_and_extract_base(base_tag)
            if not base_dir: return

            old_tree = self._get_file_tree(base_dir)
            new_tree = self._get_file_tree(Path("."))

            added = [f for f in new_tree if f not in old_tree]
            modified = [f for f in new_tree if f in old_tree and new_tree[f] != old_tree[f]]
            removed = [f for f in old_tree if f not in new_tree]

            if not (added or modified or removed): return

            # 文件名：from-{旧}-to-{新8位}-{软件Tag}.zip
            file_name_base = f"from-{base_tag}-to-{self.res_tag_short}-{self.latest_app_tag}"
            zip_name = f"{file_name_base}.zip"
            json_name = f"{file_name_base}.json"
            
            tmp_zip = PATCH_DIR / f"{zip_name}.tmp"
            tmp_json = PATCH_DIR / f"{json_name}.tmp"

            with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as z:
                for file_path in added + modified:
                    # 关键修改：如果路径以 resource/ 开头，则剥离它
                    # 原路径: resource/arknights_mower/data/version.json
                    # 存入路径: arknights_mower/data/version.json
                    p = Path(file_path)
                    if p.parts[0] == 'resource':
                        archive_name = Path(*p.parts[1:]).as_posix()
                    else:
                        archive_name = file_path
                    
                    z.write(Path(".") / file_path, archive_name)

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
            print(f"成功生成: {zip_name}")

        except Exception as e:
            print(f"生成补丁失败 {base_tag}: {e}")
            if tmp_zip.exists(): tmp_zip.unlink()

    def run(self):
        # 1. 扫描现有 JSON 提取“上一次”的资源短 Tag
        old_res_tags = set()
        for meta_file in PATCH_DIR.glob("*.json"):
            try:
                with open(meta_file, "r") as f:
                    m = json.load(f)
                    # 只有当 software_tag 还没变时，旧资源才有参考价值
                    if m.get("software_tag") == self.latest_app_tag:
                        old_res_tags.add(m.get("target_resource_short"))
            except: pass

        # 2. 清理过期文件
        self._cleanup_old_patches()

        # 3. 确定所有基准版本
        base_versions = {self.latest_app_tag} # 基础软件版
        for tag in old_res_tags:
            if tag and tag != self.res_tag_short:
                base_versions.add(tag)

        # 4. 遍历执行
        for base in base_versions:
            self.generate_patch(base)

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    PatchGenerator().run()