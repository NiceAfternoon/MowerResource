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
DATA_VERSION_FILE = SOURCE_RESOURCE_DIR / "arknights_mower/data/version.json"
RESOURCE_SUBDIRS = ["arknights_mower", "ui"]

class PatchGenerator:
    def __init__(self):
        self.session = self._build_retry_session()
        self.latest_app_tag = self._get_latest_release_tag()
        self.target_res_version = self._get_target_res_version()
        self.res_tag_short = self.target_res_version.replace(".", "")
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
        try:
            resp = self.session.get(MAIN_REPO_API, timeout=10)
            resp.raise_for_status()
            return resp.json().get("tag_name", "v0.0.0")
        except Exception as e:
            print(f"获取 App Tag 失败: {e}")
            return "v0.0.0"

    def _get_target_res_version(self):
        if not DATA_VERSION_FILE.exists():
            return "0000.0000.0000"
        with open(DATA_VERSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("res_version") or "0000.0000.0000"

    def _md5(self, filepath: Path) -> str:
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def _get_file_tree(self, root_dir: Path) -> dict:
        """
        只返回资源子目录下的文件树（相对路径以资源根为基准）。
        两种情况：
          - 开发仓库：root_dir 下存在 resource/ -> 使用 resource/ 作为 search_root
          - Release 解压：定位到 root_dir/_internal 或 root_dir/*/_internal，然后只扫描 _internal/arknights_mower 与 _internal/ui
        返回: { "arknights_mower/xxx": md5, "ui/yyy": md5, ... }
        """
        tree = {}

        # 情况 1：开发仓库资源
        if (root_dir / "resource").exists():
            search_root = root_dir / "resource"
        else:
            # 情况 2：Release 解压目录
            if (root_dir / "_internal").exists():
                search_root = root_dir / "_internal"
            else:
                # 检查是否外面套了一层（例如 GitHub zip 会多一层）
                found = None
                for sub in root_dir.iterdir():
                    if sub.is_dir() and (sub / "_internal").exists():
                        found = sub / "_internal"
                        break
                if found:
                    search_root = found
                else:
                    # 没找到 _internal，返回空树
                    return {}

        # 只扫描我们关心的两个子目录，忽略其他依赖文件
        for subdir in RESOURCE_SUBDIRS:
            target_path = search_root / subdir
            if not target_path.exists():
                continue
            for filepath in target_path.rglob("*"):
                if filepath.is_file():
                    # 相对路径以 search_root 为基准
                    rel_to_res = filepath.relative_to(search_root).as_posix()
                    # 仅记录以子目录开头的路径（防止误包含）
                    if rel_to_res.startswith(f"{subdir}/"):
                        tree[rel_to_res] = self._md5(filepath)
        return tree

    def generate_patch(self, base_tag: str):
        try:
            base_dir = self._download_and_extract_base(base_tag)
            if not base_dir:
                print(f"基准包 {base_tag} 获取失败，跳过")
                return

            old_tree = self._get_file_tree(base_dir)
            new_tree = self._get_file_tree(Path("."))

            # 只比较资源子目录下的文件（old_tree/new_tree 已保证）
            added = [f for f in new_tree if f not in old_tree]
            modified = [f for f in new_tree if f in old_tree and new_tree[f] != old_tree[f]]
            removed = [f for f in old_tree if f not in new_tree]

            if not (added or modified or removed):
                print(f"版本 {base_tag} 无变更，跳过生成")
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
                    # 额外防护：只处理以资源子目录开头的路径
                    if not any(rel_path.startswith(f"{sd}/") for sd in RESOURCE_SUBDIRS):
                        print(f"跳过非资源路径: {rel_path}")
                        continue

                    source_file = SOURCE_RESOURCE_DIR / rel_path
                    if not source_file.exists():
                        # 兼容：尝试从当前工作目录查找
                        alt_source = Path(".") / rel_path
                        if alt_source.exists():
                            source_file = alt_source
                        else:
                            print(f"警告: 源文件不存在，跳过 {rel_path}")
                            continue

                    # 最终再次确认文件确实位于资源子目录下（防止误打包）
                    try:
                        rel_check = source_file.relative_to(SOURCE_RESOURCE_DIR).as_posix()
                        if not any(rel_check.startswith(f"{sd}/") for sd in RESOURCE_SUBDIRS):
                            # 如果相对路径不在 resource/ 下，允许但仍需以资源前缀为准
                            if not any(rel_path.startswith(f"{sd}/") for sd in RESOURCE_SUBDIRS):
                                print(f"警告: 源文件不在资源目录，跳过 {source_file}")
                                continue
                    except Exception:
                        # source_file 可能不是在 SOURCE_RESOURCE_DIR 下，已通过 alt_source 处理
                        pass

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
            print(f"--- 成功生成补丁包: {zip_name} ---")

        except Exception as e:
            print(f"生成补丁失败 [{base_tag}]: {e}")

    def _download_and_extract_base(self, tag_name: str) -> Path:
        extract_path = self.tmp_dir / tag_name
        if extract_path.exists():
            return extract_path

        print(f"正在获取基准包资源: {tag_name}")
        api_url = MAIN_REPO_API if tag_name == self.latest_app_tag else f"https://api.github.com/repos/NiceAfternoon/arknights-mower/releases/tags/{tag_name}"

        resp = self.session.get(api_url, timeout=10)
        if resp.status_code != 200:
            print(f"无法找到基准版本 {tag_name} 的 Release")
            return None
        
        assets = resp.json().get("assets", [])
        dl_url, ext = None, ""
        for asset in assets:
            name = asset["name"]
            if (tag_name in name or "mower" in name.lower()) and (name.endswith(".zip") or name.endswith(".rar")):
                dl_url = asset["browser_download_url"]
                ext = Path(name).suffix
                break
        
        if not dl_url:
            print("未找到合适的压缩包资源")
            return None
        
        archive_path = self.tmp_dir / f"{tag_name}{ext}"
        with self.session.get(dl_url, stream=True) as r, open(archive_path, "wb") as f:
            shutil.copyfileobj(r.raw, f)

        if ext == ".zip":
            with zipfile.ZipFile(archive_path, 'r') as z:
                z.extractall(extract_path)
        else:
            with rarfile.RarFile(archive_path) as rf:
                rf.extractall(extract_path)

        archive_path.unlink()
        return extract_path

    def _cleanup_old_patches(self):
        for item in PATCH_DIR.glob("from-*-to-*.zip"):
            if (self.latest_app_tag not in item.name) or (self.res_tag_short not in item.name):
                print(f"清理过期补丁: {item.name}")
                item.unlink()
                json_file = item.with_suffix('.json')
                if json_file.exists():
                    json_file.unlink()

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

        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    PatchGenerator().run()
