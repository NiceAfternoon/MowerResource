import os
import json
import hashlib
import shutil
import zipfile
import requests
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
        """从主仓库获取最新的 Release Tag"""
        resp = self.session.get(MAIN_REPO_API, timeout=10)
        resp.raise_for_status()
        return resp.json().get("tag_name")

    def _get_target_res_version(self):
        """获取当前资源版本号 (last_updated)"""
        with open(DATA_VERSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("last_updated", "unknown_res")

    def _md5(self, filepath: Path) -> str:
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def _cleanup_old_patches(self):
        """清理目标版本不是当前主仓库最新 Tag 的旧包"""
        print(f"正在清理目标版本不是 {self.latest_release_tag} 的旧包...")
        # 目标匹配: from-*-to-{latest_release_tag}.zip/json
        target_suffix = f"-to-{self.latest_release_tag}"
        
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
        
        # 核心逻辑修改：寻找文件名包含该 tag 名字的 zip
        dl_url = next((a["browser_download_url"] for a in assets if real_tag in a["name"] and a["name"].endswith(".zip")), None)
        
        if not dl_url:
            raise ValueError(f"未在 Release {real_tag} 中找到包含标签名的 zip 包")

        zip_path = self.tmp_dir / f"{real_tag}.zip"
        with self.session.get(dl_url, stream=True, timeout=15) as r, open(zip_path, "wb") as f:
            r.raise_for_status()
            shutil.copyfileobj(r.raw, f)

        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(extract_path)
        
        zip_path.unlink()
        return extract_path

    def _get_file_tree(self, directory: Path) -> dict:
        tree = {}
        for filepath in directory.rglob("*"):
            if filepath.is_file() and '.git' not in filepath.parts and 'patch' not in filepath.parts:
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
                print(f"版本 {base_tag} 与当前资源库无差异，跳过生成。")
                return

            # 文件名带上主仓库的最新 Tag
            zip_name = f"from-{base_tag}-to-{self.latest_release_tag}.zip"
            json_name = f"from-{base_tag}-to-{self.latest_release_tag}.json"
            tmp_zip = PATCH_DIR / f"{zip_name}.tmp"
            tmp_json = PATCH_DIR / f"{json_name}.tmp"

            with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as z:
                for file_path in added + modified:
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
            print(f"成功生成增量包: {zip_name}")

        except Exception as e:
            print(f"为 {base_tag} 生成补丁失败: {str(e)}")
            if tmp_zip.exists(): tmp_zip.unlink()
            if tmp_json.exists(): tmp_json.unlink()
            raise

    def run(self):
        # 1. 以主仓库 Release Tag 为准清理旧包
        self._cleanup_old_patches()

        # 2. 确定所有需要生成补丁的基准版本 (Base Tags)
        # 始终包含最新的那个 tag
        base_versions = {self.latest_release_tag}
        
        # 同时从现有的 patch 文件夹中继承旧的 base_version
        for meta_file in PATCH_DIR.glob("from-*-to-*.json"):
            parts = meta_file.name.split("-")
            # 格式: from-[1]-to-[3]
            if len(parts) >= 4:
                base_versions.add(parts[1])

        # 3. 遍历生成
        for base in base_versions:
            # 如果 base 等于最新的 target 且资源没变，generate_patch 内部会跳过
            self.generate_patch(base)

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    PatchGenerator().run()