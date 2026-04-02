import os
import json
import hashlib
import shutil
import zipfile
import requests
import rarfile  # 需要 pip install rarfile
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
        self.latest_release_tag = self._get_latest_release_tag()
        self.target_res_version = self._get_target_res_version()
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
        """清理目标资源版本不一致的旧包"""
        print(f"正在清理目标版本不是 {self.target_res_version} 的旧包...")
        target_suffix = f"-to-{self.target_res_version}"
        
        for item in PATCH_DIR.glob("from-*-to-*.zip"):
            if target_suffix not in item.name:
                print(f"删除旧压缩包: {item.name}")
                item.unlink()
        for item in PATCH_DIR.glob("from-*-to-*.json"):
            if target_suffix not in item.name:
                print(f"删除旧元数据: {item.name}")
                item.unlink()

    def _download_and_extract_base(self, tag_name: str) -> Path:
        extract_path = self.tmp_dir / tag_name
        if extract_path.exists():
            return extract_path

        print(f"获取基础版本全量包: {tag_name}")
        api_url = f"https://api.github.com/repos/ArkMowers/arknights-mower/releases/tags/{tag_name}"
        if tag_name == self.latest_release_tag:
            api_url = MAIN_REPO_API

        resp = self.session.get(api_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        real_tag = data.get("tag_name")
        assets = data.get("assets", [])
        
        dl_url = None
        ext = ""
        for asset in assets:
            name = asset["name"]
            if real_tag in name and (name.endswith(".rar") or name.endswith(".zip")):
                dl_url = asset["browser_download_url"]
                ext = ".rar" if name.endswith(".rar") else ".zip"
                break
        
        if not dl_url:
            raise ValueError(f"未在 Release {real_tag} 中找到对应的压缩包")

        archive_path = self.tmp_dir / f"{real_tag}{ext}"
        with self.session.get(dl_url, stream=True, timeout=15) as r, open(archive_path, "wb") as f:
            r.raise_for_status()
            shutil.copyfileobj(r.raw, f)

        if ext == ".zip":
            with zipfile.ZipFile(archive_path, 'r') as z:
                z.extractall(extract_path)
        else:
            with rarfile.RarFile(archive_path) as rf:
                rf.extractall(extract_path)

        archive_path.unlink()
        return extract_path

    def _get_file_tree(self, directory: Path) -> dict:
        """生成目录树，严格排除临时文件和无关目录"""
        tree = {}
        # 必须排除的目录名
        exclude_dirs = {'.git', 'patch', '.tmp_patch_build', '__pycache__'}
        
        for filepath in directory.rglob("*"):
            # 如果路径中包含任何需要排除的目录，则跳过
            if any(part in filepath.parts for part in exclude_dirs):
                continue
                
            if filepath.is_file():
                rel_path = filepath.relative_to(directory).as_posix()
                tree[rel_path] = self._md5(filepath)
        return tree

    def generate_patch(self, base_tag: str):
        tmp_zip, tmp_json = Path(), Path()
        try:
            base_dir = self._download_and_extract_base(base_tag)
            current_dir = Path(".") 
            
            old_tree = self._get_file_tree(base_dir)
            new_tree = self._get_file_tree(current_dir)

            added = [f for f in new_tree if f not in old_tree]
            modified = [f for f in new_tree if f in old_tree and new_tree[f] != old_tree[f]]
            removed = [f for f in old_tree if f not in new_tree]

            if not (added or modified or removed):
                print(f"软件版本 {base_tag} 对应的资源与当前无差异，跳过。")
                return

            zip_name = f"from-{base_tag}-to-{self.target_res_version}.zip"
            json_name = f"from-{base_tag}-to-{self.target_res_version}.json"
            tmp_zip = PATCH_DIR / f"{zip_name}.tmp"
            tmp_json = PATCH_DIR / f"{json_name}.tmp"

            with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as z:
                for file_path in added + modified:
                    # 再次确保不把 patch 目录或临时目录打包进去
                    z.write(current_dir / file_path, file_path)

            meta = {
                "base_version": base_tag,
                "target_software_version": self.latest_release_tag,
                "target_resource_version": self.target_res_version,
                "files_added": added,
                "files_modified": modified,
                "files_removed": removed,
                "md5": self._md5(tmp_zip),
                "size": tmp_zip.stat().st_size,
                "created_at": datetime.utcnow().isoformat() + "Z"
            }
            with open(tmp_json, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

            os.replace(tmp_zip, PATCH_DIR / zip_name)
            os.replace(tmp_json, PATCH_DIR / json_name)
            print(f"成功生成增量包: {zip_name} (Size: {meta['size']})")

        except Exception as e:
            print(f"为 {base_tag} 生成补丁失败: {str(e)}")
            if tmp_zip.exists(): tmp_zip.unlink()
            if tmp_json.exists(): tmp_json.unlink()
            raise

    def run(self):
        # 1. 以资源版本为基准清理
        self._cleanup_old_patches()
        
        # 2. 收集需要对比的软件版本
        base_versions = {self.latest_release_tag}
        # 扫描 patch 目录，找出历史遗留的 base 版本
        for meta_file in PATCH_DIR.glob("from-*-to-*.json"):
            parts = meta_file.name.split("-")
            if len(parts) >= 2:
                # 格式: from-[1]-to-...
                base_versions.add(parts[1])

        for base in base_versions:
            self.generate_patch(base)

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    PatchGenerator().run()