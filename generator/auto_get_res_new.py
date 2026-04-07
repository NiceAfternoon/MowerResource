import argparse
import json
import lzma
import os
import pickle
import hashlib
import re
from datetime import datetime, timezone, timedelta
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from skimage.feature import hog
from sklearn.neighbors import KNeighborsClassifier

from image import loadimg, thres2

# 命令行参数
parser = argparse.ArgumentParser()
parser.add_argument("--local-only", action="store_true", help="仅执行云端跳过的本地专项训练任务")
args = parser.parse_args()

# 检测是否在 Workflow 环境下运行
IS_WORKFLOW = os.getenv("GITHUB_ACTIONS") == "true"

# 指定输出目录
current_script_path = os.path.abspath(__file__)
current_dir = os.path.dirname(current_script_path)
project_root = os.path.dirname(current_dir)
RESOURCE_ROOT = os.path.join(project_root, "resource")

def skip_in_workflow(func):
    """仅依赖非开源字体的函数，在 Workflow 中无条件跳过"""
    def wrapper(*args, **kwargs):
        if IS_WORKFLOW:
            print(f"检测到当前为 Workflow 环境，已跳过依赖非开源字体的任务: {func.__name__}")
            return None
        return func(*args, **kwargs)
    return wrapper

def skip_if_no_font(func):
    """依赖开源字体的函数，若字体存在则执行，否则跳过"""
    def wrapper(*args, **kwargs):
        font_path = os.path.join(RESOURCE_ROOT, "arknights_mower", "fonts", "SourceHanSansCN-Medium.otf")
        if not os.path.exists(font_path):
            print(f"未检测到开源字体文件，已跳过: {func.__name__}")
            return None
        return func(*args, **kwargs)
    return wrapper

def 提取干员名图片(imgpath, 裁剪区域: int = 1, 模式: int = 1):
    pos = {
        "常规站": {"左上": (631, 488), "左下": (631, 909)},
        "训练室": {"左上": (578, 479), "左下": (578, 895)},
    }
    shape = {"常规站": (190, 32), "训练室": (180, 27)}
    img = Image.open(imgpath)
    站 = "常规站" if 模式 == 1 else "训练室"
    位置 = "左上" if 裁剪区域 == 1 else "左下"
    (x, y) = pos[站][位置]
    (w, h) = shape[站]
    img = img.crop((x, y, x + w, y + h))
    
    save_dir = f"{RESOURCE_ROOT}/arknights_mower/opname"
    os.makedirs(save_dir, exist_ok=True)
    img.save(f"{save_dir}/unknown.png")


class Arknights数据处理器:
    def __init__(self):
        self.init_dirs()
        self.当前时间戳 = datetime.now().timestamp()
        self.物品表 = self.加载json("./ArknightsGameResource/gamedata/excel/item_table.json")
        self.干员表 = self.加载json("./ArknightsGameResource/gamedata/excel/character_table.json")
        self.抽卡表 = self.加载json("./ArknightsGameResource/gamedata/excel/gacha_table.json")
        self.关卡表 = self.加载json("./ArknightsGameResource/gamedata/excel/stage_table.json")
        self.活动表 = self.加载json("./ArknightsGameResource/gamedata/excel/activity_table.json")
        self.基建表 = self.加载json("./ArknightsGameResource/gamedata/excel/building_data.json")
        self.游戏变量 = self.加载json("./ArknightsGameResource/gamedata/excel/gamedata_const.json")
        self.装仓库物品的字典 = {"NORMAL": [], "CONSUME": [], "MATERIAL": []}

        stage_data_path = f"{RESOURCE_ROOT}/arknights_mower/data/stage_data.json"
        if os.path.exists(stage_data_path):
            self.常驻关卡 = self.加载json(stage_data_path)
        else:
            self.常驻关卡 = []
            
        self.所有buff = []

        self.限定十连 = self.抽卡表["limitTenGachaItem"]
        self.联动十连 = self.抽卡表["linkageTenGachaItem"]
        self.普通十连 = self.抽卡表["normalGachaItem"]
        self.所有卡池 = self.限定十连 + self.联动十连 + self.普通十连

    def init_dirs(self):
        directories = [
            f"{RESOURCE_ROOT}/arknights_mower/opname",
            f"{RESOURCE_ROOT}/arknights_mower/data",
            f"{RESOURCE_ROOT}/arknights_mower/models",
            f"{RESOURCE_ROOT}/arknights_mower/fonts",
            f"{RESOURCE_ROOT}/ui/public/depot",
            f"{RESOURCE_ROOT}/ui/public/avatar",
            f"{RESOURCE_ROOT}/ui/public/building_skill",
            f"{RESOURCE_ROOT}/ui/src/pages/stage_data",
            f"{RESOURCE_ROOT}/ui/src/pages/basement_skill",
        ]
        for directory in directories:
            os.makedirs(directory, exist_ok=True)

    def 加载json(self, file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def 添加物品(self):
        def 检查图标代码匹配(目标图标代码, 物品类型):
            匹配结果 = False
            for 池子限时物品 in self.所有卡池:
                if (池子限时物品["itemId"] == 目标图标代码 and self.当前时间戳 > 池子限时物品["endTime"]):
                    匹配结果 = True
                    break
            分割部分 = 目标图标代码.split("_")
            if len(分割部分) == 2 and 分割部分[0].endswith("recruitment10"):
                匹配结果 = True
            if len(分割部分) == 6 and int(分割部分[5]) < 2023:
                匹配结果 = True
            if len(分割部分) == 3 and 目标图标代码.startswith("uni"):
                匹配结果 = True
            if len(分割部分) == 3 and 目标图标代码.startswith("voucher_full"):
                匹配结果 = True
            if 目标图标代码 == "ap_supply_lt_60":
                匹配结果 = True
            抽卡 = self.抽卡表.get("gachaPoolClient", [])
            for 卡池 in 抽卡:
                if 卡池["LMTGSID"] == 目标图标代码 and self.当前时间戳 > int(卡池["endTime"]):
                    匹配结果 = True
            return 匹配结果

        self.物品_名称对 = {}
        exp_webp_path = f"{RESOURCE_ROOT}/ui/public/depot/EXP.webp"
        if not os.path.exists(exp_webp_path):
            png_image = Image.open("./ArknightsGameResource/item/EXP_PLAYER.png")
            png_image.save(exp_webp_path, "WEBP")
            
        for 物品代码, 物品数据 in self.物品表["items"].items():
            中文名称 = 物品数据.get("name", "")
            图标代码 = 物品数据.get("iconId", "")
            排序代码 = 物品数据.get("sortId", "")
            分类类型 = 物品数据.get("classifyType", "")
            物品类型 = 物品数据.get("itemType", "")
            源文件路径 = f"./ArknightsGameResource/item/{图标代码}.png"
            排除开关 = 检查图标代码匹配(图标代码, 物品类型)
            
            if 分类类型 != "NONE" and 排序代码 > 0 and not 排除开关:
                if os.path.exists(源文件路径):
                    目标文件路径 = f"{RESOURCE_ROOT}/ui/public/depot/{中文名称}.webp"
                    self.装仓库物品的字典[分类类型].append([目标文件路径, 源文件路径])
                    if not os.path.exists(目标文件路径):
                        png_image = Image.open(源文件路径)
                        png_image.save(目标文件路径, "WEBP")
                    templist = [物品代码, 图标代码, 中文名称, 分类类型, 排序代码]
                    self.物品_名称对[物品代码] = templist
                    self.物品_名称对[中文名称] = templist
                    print(f"复制 {源文件路径} 到 {目标文件路径}")
                else:
                    print(f"可以复制，但是未找到: {源文件路径}")
                    
        with open(f"{RESOURCE_ROOT}/arknights_mower/data/key_mapping.json", "w", encoding="utf-8") as json_file:
            json.dump(self.物品_名称对, json_file, ensure_ascii=False, indent=4)

    def 添加干员(self):
        干员_名称列表 = []
        干员_职业列表 = {}
        for 干员代码, 干员数据 in self.干员表.items():
            if not 干员数据["itemObtainApproach"]:
                continue
            干员名 = 干员数据["name"]
            干员_名称列表.append(干员名)
            干员_职业列表[干员名] = 干员数据["profession"]
            干员头像路径 = f"./ArknightsGameResource/avatar/{干员代码}.png"
            目标路径 = f"{RESOURCE_ROOT}/ui/public/avatar/{干员数据['name']}.webp"
            try:
                png_image = Image.open(干员头像路径)
                png_image.save(目标路径, "WEBP")
            except Exception:
                pass
        干员_名称列表.sort(key=len)
        with open(f"{RESOURCE_ROOT}/arknights_mower/data/agent_profession.json", "w", encoding="utf-8") as f:
            json.dump(干员_职业列表, f, ensure_ascii=False)
        with open(f"{RESOURCE_ROOT}/arknights_mower/data/agent.json", "w", encoding="utf-8") as f:
            json.dump(干员_名称列表, f, ensure_ascii=False)

    def 读取卡池(self):
        # 原逻辑中的 print 已全部移除，核心信息提取已整合至 generate_version_info 中
        pass

    def 读取活动关卡(self):
        关卡 = self.关卡表["stageValidInfo"]
        还未结束的非常驻关卡 = {
            键: 值
            for 键, 值 in 关卡.items()
            if 值["endTs"] != -1 and 值["endTs"] > self.当前时间戳
        }
        所有关卡 = []
        还未结束的非常驻关卡 = dict(sorted(还未结束的非常驻关卡.items()))
        zones = self.加载json("./ArknightsGameResource/gamedata/excel/zone_table.json")["zones"]
        story_review = {
            k: v["name"]
            for k, v in self.加载json("./ArknightsGameResource/gamedata/excel/story_review_table.json").items()
        }
        activity_table = self.加载json("./ArknightsGameResource/gamedata/excel/activity_table.json")
        zoneToActivity = activity_table.get("zoneToActivity", {})
        activityBasicInfo = activity_table.get("basicInfo", {})

        def _pick_text(*values):
            for value in values:
                if isinstance(value, str):
                    value = value.strip()
                    if value: return value
            return ""

        def clean_zone_name(name):
            if not isinstance(name, str): return name
            return name.replace("·复刻", "").strip()

        def get_zone_name(zone_id):
            zone = zones.get(zone_id, {})
            return _pick_text(
                zone.get("zoneNameSecond"), zone.get("zoneNameFirst"), zone.get("zoneNameThird"),
                zone.get("zoneNameTitleCurrent"), zone.get("zoneNameTitleUnCurrent"), zone.get("zoneID"), zone_id
            )

        def get_activity_name(activity_id):
            if not activity_id: return ""
            info = activityBasicInfo.get(activity_id, {})
            return clean_zone_name(_pick_text(info.get("name"), info.get("id"), activity_id))
            
        for 键, _ in 还未结束的非常驻关卡.items():
            关卡代码 = self.关卡表["stages"][键]["code"]
            if 键.endswith("#f#"): 关卡代码 += " 突袭"
            关卡名称 = self.关卡表["stages"][键]["name"]
            关卡结束时间戳 = 还未结束的非常驻关卡[键]["endTs"]
            关卡掉落表 = self.关卡表["stages"][键]["stageDropInfo"]["displayDetailRewards"]

            突袭首次掉落 = [self.物品表.get("items", {}).get(item["id"], {}).get("name", item["id"]) for item in 关卡掉落表 if item["dropType"] == 1]
            常规掉落 = [self.物品表.get("items", {}).get(item["id"], {}).get("name", item["id"]) for item in 关卡掉落表 if item["dropType"] == 2]
            特殊掉落 = [self.物品表.get("items", {}).get(item["id"], {}).get("name", item["id"]) for item in 关卡掉落表 if item["dropType"] == 3]
            额外物资 = [self.物品表.get("items", {}).get(item["id"], {}).get("name", item["id"]) for item in 关卡掉落表 if item["dropType"] == 4]
            首次掉落 = [self.物品表.get("items", {}).get(item["id"], {}).get("name", item["id"]) for item in 关卡掉落表 if item["dropType"] == 8]
            
            关卡掉落 = {"突袭首次掉落": 突袭首次掉落, "常规掉落": 常规掉落, "首次掉落": 首次掉落, "特殊掉落": 特殊掉落, "额外物资": 额外物资}
            值 = self.关卡表["stages"][键]
            if (值["zoneId"] in zones and 值["levelId"] and any(part in story_review for part in 值["levelId"].split("/"))):
                event_name = next((story_review[part] for part in 值["levelId"].split("/") if part in story_review))
                所有关卡.append({
                    "id": 关卡代码, "name": 关卡名称, "drop": 关卡掉落表, "zoneId": 值["zoneId"],
                    "apCost": 值["apCost"], "difficulty": 值["difficulty"], "diffGroup": 值["diffGroup"],
                    "zoneNameSecond": clean_zone_name(event_name),
                    "subTitle": get_zone_name(值["zoneId"]) if 值["zoneId"] in zones else "",
                    "stageType": 值["stageType"], "endTs": _
                })

            self.常驻关卡.append({
                "id": 关卡代码, "name": 关卡名称, "drop": 关卡掉落, "end": 关卡结束时间戳,
                "周一": 1, "周二": 1, "周三": 1, "周四": 1, "周五": 1, "周六": 1, "周日": 1,
            })
            
        for unkey, item in enumerate(self.常驻关卡):
            item["key"] = unkey
            
        with open(f"{RESOURCE_ROOT}/ui/src/pages/stage_data/event_data.json", "w", encoding="utf-8") as f:
            json.dump(self.常驻关卡, f, ensure_ascii=False, indent=2)
            
        普通关卡 = self.关卡表["stages"]
        storylineStorySets = self.关卡表["storylineStorySets"]
        ssData = {}
        全部关卡排序信息 = []
        for k, v in storylineStorySets.items():
            if "ssData" in v and v["ssData"] and "reopenActivityId" in v["ssData"]:
                ssData[v["ssData"]["reopenActivityId"]] = v["ssData"]
            name = ""
            if v.get("mainlineData") and v["mainlineData"].get("zoneId"):
                name = get_zone_name(v["mainlineData"]["zoneId"])
            elif v.get("ssData") and v["ssData"].get("name"):
                name = v["ssData"]["name"]
            elif v.get("collectData") and v["collectData"].get("name"):
                name = v["collectData"]["name"]
            if not name and v.get("relevantActivityId"):
                name = get_activity_name(v["relevantActivityId"])
            if not name and v.get("ssData") and v["ssData"].get("reopenActivityId"):
                name = get_activity_name(v["ssData"]["reopenActivityId"])
            全部关卡排序信息.append({"name": name, "sortByYear": v.get("sortByYear"), "sortWithinYear": v.get("sortWithinYear")})

        for 键, 值 in 普通关卡.items():
            关卡代码 = 值["code"]
            关卡名称 = 值["name"]
            关卡掉落表 = None
            关卡ZONE = 值["zoneId"]
            关卡AP = 值["apCost"]
            if 值["stageType"] == "MAIN" and 关卡ZONE in zones:
                所有关卡.append({
                    "id": 关卡代码, "name": 关卡名称, "drop": 关卡掉落表, "zoneId": 关卡ZONE,
                    "apCost": 关卡AP, "difficulty": 值["difficulty"], "diffGroup": 值["diffGroup"],
                    "zoneNameSecond": clean_zone_name(get_zone_name(关卡ZONE)), "stageType": 值["stageType"],
                })
            elif 值["stageType"] == "DAILY":
                所有关卡.append({
                    "id": 关卡代码, "name": 值["code"], "drop": 关卡掉落表, "zoneId": 值["zoneId"],
                    "apCost": 值["apCost"], "difficulty": 值["difficulty"], "diffGroup": 值["diffGroup"],
                    "zoneNameSecond": clean_zone_name(get_zone_name(值["zoneId"])) if 值["zoneId"] in zones else "",
                    "stageType": 值["stageType"],
                })
            elif 值["zoneId"] in zoneToActivity and 值["stageType"] == "ACTIVITY":
                activity_id = zoneToActivity[值["zoneId"]]
                activity_name = get_activity_name(activity_id)
                if not activity_name and activity_id in ssData:
                    activity_name = _pick_text(ssData[activity_id].get("name"))
                所有关卡.append({
                    "id": 关卡代码, "name": 关卡名称, "drop": 关卡掉落表, "zoneId": 关卡ZONE,
                    "apCost": 关卡AP, "difficulty": 值["difficulty"], "diffGroup": 值["diffGroup"],
                    "zoneNameSecond": clean_zone_name(activity_name),
                    "subTitle": get_zone_name(关卡ZONE) if 关卡ZONE in zones else "",
                    "stageType": 值["stageType"],
                })
        with open(f"{RESOURCE_ROOT}/arknights_mower/data/stage_data_full.json", "w", encoding="utf-8") as f:
            json.dump(所有关卡, f, ensure_ascii=False, indent=2)
        with open(f"{RESOURCE_ROOT}/arknights_mower/data/stage_order.json", "w", encoding="utf-8") as f:
            json.dump(全部关卡排序信息, f, ensure_ascii=False, indent=4)

    def load_recruit_data(self):
        recruit_data = {}
        recruit_result_data = {4: [], 3: [], 2: [], 1: [], -1: []}
        recruit_list = self.抽卡表["recruitDetail"].replace("\\n<@rc.eml>", "").replace("\\n", "").replace("\r", "")
        for target in ["★", "<@rc.eml>", "</>", "/", "--------------------", "<@rc.title>公开招募说明", 
                       "<@rc.em>※稀有职业需求招募说明※", "<@rc.em>当职业需求包含高级资深干员，且招募时限为9小时时，招募必得6星干员",
                       "<@rc.em>当职业需求包含资深干员同时不包含高级资深干员，且招募时限为9小时，则该次招募必得5星干员",
                       "<@rc.subtitle>※全部可能出现的干员※", "绿色高亮的不可寻访干员，可以在此招募"]:
            recruit_list = recruit_list.replace(target, "")
        recruit_list = recruit_list.replace(" ", "\n").split("\n")

        profession = {"MEDIC": "医疗干员", "WARRIOR": "近卫干员", "SPECIAL": "特种干员", "SNIPER": "狙击干员", 
                      "CASTER": "术师干员", "TANK": "重装干员", "SUPPORT": "辅助干员", "PIONEER": "先锋干员"}

        for 干员代码, 干员数据 in self.干员表.items():
            干员名 = 干员数据["name"]
            if 干员数据["profession"] not in profession: continue

            if 干员名 in recruit_list:
                tag = 干员数据["tagList"]
                干员数据["rarity"] = 干员数据["rarity"] + 1
                if len(干员名) <= 4:
                    recruit_result_data[len(干员名)].append(干员代码)
                else:
                    recruit_result_data[-1].append(干员代码)
                if 干员数据["rarity"] == 5: tag.append("资深干员")
                elif 干员数据["rarity"] == 6: tag.append("高级资深干员")
                
                if 干员数据["position"] == "MELEE": tag.append("近战位")
                elif 干员数据["position"] == "RANGED": tag.append("远程位")

                tag.append(profession[干员数据["profession"]])
                recruit_data[干员代码] = {"name": 干员名, "stars": 干员数据["rarity"], "tags": 干员数据["tagList"]}
                
        with open(f"{RESOURCE_ROOT}/arknights_mower/data/recruit.json", "w", encoding="utf-8") as f:
            json.dump(recruit_data, f, ensure_ascii=False, indent=4)
        with open(f"{RESOURCE_ROOT}/arknights_mower/data/recruit_result.json", "w", encoding="utf-8") as f:
            json.dump(recruit_result_data, f, ensure_ascii=False, indent=4)

    @skip_in_workflow
    def load_recruit_template(self):
        template = {}
        with open(f"{RESOURCE_ROOT}/arknights_mower/data/recruit.json", "r", encoding="utf-8") as f:
            recruit_operators = json.load(f)
            
        font = ImageFont.truetype("FZDYSK.TTF", 120)
        for operator in recruit_operators:
            im = Image.new(mode="RGBA", size=(1920, 1080))
            draw = ImageDraw.Draw(im)
            draw.text((0, 0), recruit_operators[operator]["name"], font=font)
            im = im.crop(im.getbbox())
            im = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2GRAY)
            template[operator] = im

        with lzma.open(f"{RESOURCE_ROOT}/arknights_mower/models/recruit_result.pkl", "wb") as f:
            pickle.dump(template, f)

    @skip_if_no_font
    def load_recruit_tag(self):
        with open(f"{RESOURCE_ROOT}/arknights_mower/data/recruit.json", "r", encoding="utf-8") as f:
            recruit_agent = json.load(f)

        font = ImageFont.truetype(f"{RESOURCE_ROOT}/arknights_mower/fonts/SourceHanSansCN-Medium.otf", 30)
        recruit_tag = ["资深干员", "高级资深干员"]
        recruit_tag_template = {}
        for x in recruit_agent.values():
            recruit_tag += x["tags"]
        recruit_tag = list(set(recruit_tag))
        for tag in recruit_tag:
            im = Image.new(mode="RGBA", color=(49, 49, 49), size=(215, 70))
            W, H = im.size
            draw = ImageDraw.Draw(im)
            _, _, w, h = draw.textbbox((0, 0), tag, font=font)
            draw.text(((W - w) / 2, (H - h) / 2 - 5), tag, font=font)
            recruit_tag_template[tag] = cv2.cvtColor(np.array(im.crop(im.getbbox())), cv2.COLOR_RGB2BGR)
        with lzma.open(f"{RESOURCE_ROOT}/arknights_mower/models/recruit.pkl", "wb") as f:
            pickle.dump(recruit_tag_template, f)

    def load_recruit_resource(self):
        self.load_recruit_data()
        self.load_recruit_template()
        self.load_recruit_tag()

    def 训练仓库的knn模型(self, 模板文件夹, 模型保存路径):
        def 提取特征点(模板):
            模板 = 模板[40:173, 40:173]
            hog_features = hog(模板, orientations=18, pixels_per_cell=(16, 16),
                               cells_per_block=(2, 2), block_norm="L2-Hys", transform_sqrt=True, channel_axis=2)
            return hog_features

        def 加载图片特征点_标签(模板类型):
            特征点列表, 标签列表 = [], []
            for [目标文件路径, 源文件路径] in self.装仓库物品的字典[模板类型]:
                模板 = cv2.imread(源文件路径)
                模板 = cv2.resize(模板, (213, 213))
                特征点 = 提取特征点(模板)
                特征点列表.append(特征点)
                标签列表.append(self.物品_名称对[目标文件路径.split("/")[-1].replace(".webp", "")][2])
            return 特征点列表, 标签列表

        模板特征点, 模板标签 = 加载图片特征点_标签(模板文件夹)
        knn模型 = KNeighborsClassifier(weights="distance", n_neighbors=1, n_jobs=-1)
        knn模型.fit(模板特征点, 模板标签)
        with lzma.open(模型保存路径, "wb") as f:
            pickle.dump(knn模型, f)

    def 批量训练并保存扫仓库模型(self):
        self.训练仓库的knn模型("NORMAL", f"{RESOURCE_ROOT}/arknights_mower/models/NORMAL.pkl")
        self.训练仓库的knn模型("CONSUME", f"{RESOURCE_ROOT}/arknights_mower/models/CONSUME.pkl")

    @skip_if_no_font
    def 训练在房间内的干员名的模型(self):
        font = ImageFont.truetype(f"{RESOURCE_ROOT}/arknights_mower/fonts/SourceHanSansCN-Medium.otf", 37)
        data = {}
        kernel = np.ones((12, 12), np.uint8)

        with open(f"{RESOURCE_ROOT}/arknights_mower/data/agent.json", "r", encoding="utf-8") as f:
            agent_list = json.load(f)
        for operator in sorted(agent_list, key=lambda x: len(x), reverse=True):
            img = Image.new(mode="L", size=(400, 100))
            draw = ImageDraw.Draw(img)
            draw.text((50, 20), operator, fill=(255,), font=font)
            img = np.array(img, dtype=np.uint8)
            img = thres2(img, 200)
            dilation = cv2.dilate(img, kernel, iterations=1)
            contours, _ = cv2.findContours(dilation, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
            rect = map(lambda c: cv2.boundingRect(c), contours)
            x, y, w, h = sorted(rect, key=lambda c: c[0])[0]
            img = img[y : y + h, x : x + w]
            tpl = np.zeros((46, 265), dtype=np.uint8)
            tpl[: img.shape[0], : img.shape[1]] = img
            data[operator] = tpl

        with lzma.open(f"{RESOURCE_ROOT}/arknights_mower/models/operator_room.model", "wb") as f:
            pickle.dump(data, f)

    @skip_if_no_font
    def 训练选中的干员名的模型(self):
        font31 = ImageFont.truetype(f"{RESOURCE_ROOT}/arknights_mower/fonts/SourceHanSansCN-Medium.otf", 31)
        font30 = ImageFont.truetype(f"{RESOURCE_ROOT}/arknights_mower/fonts/SourceHanSansCN-Medium.otf", 30)
        font27 = ImageFont.truetype(f"{RESOURCE_ROOT}/arknights_mower/fonts/SourceHanSansCN-Medium.otf", 27)
        font25 = ImageFont.truetype(f"{RESOURCE_ROOT}/arknights_mower/fonts/SourceHanSansCN-Medium.otf", 25)
        font23 = ImageFont.truetype(f"{RESOURCE_ROOT}/arknights_mower/fonts/SourceHanSansCN-Medium.otf", 23)

        data = {}
        kernel = np.ones((10, 10), np.uint8)

        with open(f"{RESOURCE_ROOT}/arknights_mower/data/agent.json", "r", encoding="utf-8") as f:
            agent_list = json.load(f)
        for idx, operator in enumerate(agent_list):
            font = font31
            if not operator[0].encode().isalpha():
                if len(operator) == 7:
                    font = font27 if "·" in operator else font25
                elif operator == "Miss.Christine":
                    font = font23
                elif len(operator) == 6:
                    font = font30
            img = Image.new(mode="L", size=(400, 100))
            draw = ImageDraw.Draw(img)
            if "·" in operator:
                x, y = 50, 20
                char_index = {i: False for i, char in enumerate(operator) if char == "·"}
                for i, char in enumerate(operator):
                    if i in char_index and not char_index[i]:
                        x -= 8
                        char_index[i] = True
                        if i + 1 not in char_index and char == "·":
                            char_index[i + 1] = False
                    draw.text((x, y), char, fill=(255,), font=font)
                    x += font.getbbox(char)[2:4][0]
            elif operator == "Miss.Christine":
                x, y = 50, 20
                for i, char in enumerate(operator):
                    draw.text((x, y), char, fill=(255,), font=font)
                    x += font.getbbox(char)[2:4][0] - 1
            else:
                draw.text((50, 20), operator, fill=(255,), font=font)

            img = np.array(img, dtype=np.uint8)
            local_imgpath = f"{RESOURCE_ROOT}/arknights_mower/opname/{operator}.png"
            if os.path.exists(local_imgpath):
                img_array = np.frombuffer(open(local_imgpath, "rb").read(), np.uint8)
                img = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
            img = thres2(img, 140)
            dilation = cv2.dilate(img, kernel, iterations=1)
            contours, _ = cv2.findContours(dilation, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
            rect = map(lambda c: cv2.boundingRect(c), contours)
            x, y, w, h = sorted(rect, key=lambda c: c[0])[0]
            img = img[y : y + h, x : x + w]
            tpl = np.zeros((42, 200), dtype=np.uint8)
            tpl[: img.shape[0], : img.shape[1]] = img
            data[operator] = tpl

        with lzma.open(f"{RESOURCE_ROOT}/arknights_mower/models/operator_select.model", "wb") as f:
            pickle.dump(data, f)

    @skip_if_no_font
    def 训练训练室干员名的模型(self):
        font30 = ImageFont.truetype(f"{RESOURCE_ROOT}/arknights_mower/fonts/SourceHanSansCN-Medium.otf", 30)
        font28 = ImageFont.truetype(f"{RESOURCE_ROOT}/arknights_mower/fonts/SourceHanSansCN-Medium.otf", 28)
        font25 = ImageFont.truetype(f"{RESOURCE_ROOT}/arknights_mower/fonts/SourceHanSansCN-Medium.otf", 25)
        font24 = ImageFont.truetype(f"{RESOURCE_ROOT}/arknights_mower/fonts/SourceHanSansCN-Medium.otf", 24)

        data = {}
        kernel = np.ones((10, 10), np.uint8)

        with open(f"{RESOURCE_ROOT}/arknights_mower/data/agent.json", "r", encoding="utf-8") as f:
            agent_list = json.load(f)
        for idx, operator in enumerate(agent_list):
            font = font30
            if not operator[0].encode().isalpha():
                if len(operator) == 7:
                    font = font25 if "·" in operator else font24
                elif len(operator) == 6:
                    font = font28
            img = Image.new(mode="L", size=(400, 100))
            draw = ImageDraw.Draw(img)
            draw.text((50, 20), operator, fill=(255,), font=font)

            img = np.array(img, dtype=np.uint8)
            local_imgpath = f"{RESOURCE_ROOT}/arknights_mower/opname/{operator}_train.png"
            if os.path.exists(local_imgpath):
                img_array = np.frombuffer(open(local_imgpath, "rb").read(), np.uint8)
                img = cv2.imdecode(img_array, cv2.IMREAD_GRAYSCALE)
            img = thres2(img, 140)
            dilation = cv2.dilate(img, kernel, iterations=1)
            contours, _ = cv2.findContours(dilation, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
            rect = map(lambda c: cv2.boundingRect(c), contours)
            x, y, w, h = sorted(rect, key=lambda c: c[0])[0]
            img = img[y : y + h, x : x + w]
            tpl = np.zeros((42, 200), dtype=np.uint8)
            h = min(img.shape[0], tpl.shape[0])
            w = min(img.shape[1], tpl.shape[1])
            tpl[:h, :w] = img[:h, :w]
            data[operator] = tpl

        with lzma.open(f"{RESOURCE_ROOT}/arknights_mower/models/operator_train.model", "wb") as f:
            pickle.dump(data, f)

    def auto_fight_avatar(self):
        avatar_mapping = {name: data["name"] for name, data in self.干员表.items()}
        avatar = {}
        avatar_path = "./ArknightsGameResource/avatar"
        for i in os.listdir(avatar_path):
            for j, k in avatar_mapping.items():
                if i.startswith(j):
                    img = loadimg(os.path.join(avatar_path, i), True)
                    img = cv2.resize(img, None, None, 0.5, 0.5)
                    if k not in avatar: avatar[k] = []
                    avatar[k].append(img)
                    break
        with lzma.open(f"{RESOURCE_ROOT}/arknights_mower/models/avatar.pkl", "wb") as f:
            pickle.dump(avatar, f)

    def 获得干员基建描述(self):
        buff描述 = self.基建表["buffs"]
        buff_table = {
            buff名称: [相关buff["buffName"], 相关buff["description"], 相关buff["roomType"],
                     相关buff["buffCategory"], 相关buff["skillIcon"], 相关buff["buffColor"], 相关buff["textColor"]]
            for buff名称, 相关buff in buff描述.items()
        }

        干员技能列表 = []
        name_key = 0
        for 角色id, 相关buff in self.基建表["chars"].items():
            干员技能字典 = {"key": 0, "name": self.干员表[角色id]["name"], "span": 0, "child_skill": []}
            skill_key = 0
            name_key += 1
            干员技能字典["key"] = name_key
            for item in 相关buff["buffChar"]:
                skill_level = 0
                if item["buffData"]:
                    for item2 in item["buffData"]:
                        干员技能详情 = {
                            "skill_key": skill_key,
                            "skill_level": skill_level,
                            "phase_level": f"精{item2['cond']['phase']} {item2['cond']['level']}级",
                            "skillname": buff_table[item2["buffId"]][0]
                        }
                        skill_level += 1
                        text = buff_table[item2["buffId"]][1]
                        matches = re.findall(r"<\$(.*?)>", text)
                        干员技能详情["buffer"] = bool(matches)
                        干员技能详情["buffer_des"] = sorted(list(set([m.replace(".", "_") for m in matches]))) if matches else []
                        if matches: self.所有buff.extend(干员技能详情["buffer_des"])

                        干员技能详情.update({
                            "des": text,
                            "roomType": roomType[buff_table[item2["buffId"]][2]],
                            "buffCategory": buff_table[item2["buffId"]][3],
                            "skillIcon": buff_table[item2["buffId"]][4],
                            "buffColor": buff_table[item2["buffId"]][5],
                            "textColor": buff_table[item2["buffId"]][6]
                        })
                        干员技能字典["child_skill"].append(干员技能详情)
                    干员技能字典["span"] = len(干员技能字典["child_skill"])
                skill_key += 1
            干员技能列表.append(干员技能字典.copy())
            
        干员技能列表 = sorted(干员技能列表, key=lambda x: -x["key"])
        with open(f"{RESOURCE_ROOT}/ui/src/pages/basement_skill/skill.json", "w", encoding="utf-8") as f:
            json.dump(干员技能列表, f, ensure_ascii=False, indent=2)

    def buff转换(self):
        buff_table = {}
        for item in self.游戏变量["termDescriptionDict"]:
            matches = re.findall(r"<\$(.*?)>", self.游戏变量["termDescriptionDict"][item]["description"])
            matches = [match.replace(".", "_") for match in matches]
            dict1 = self.游戏变量["termDescriptionDict"][item]
            dict1["buffer"] = matches if item.startswith("cc") and matches else []
            buff_table[item.replace(".", "_")] = dict1

        with open(f"{RESOURCE_ROOT}/ui/src/pages/basement_skill/buffer.json", "w", encoding="utf-8") as f:
            json.dump(buff_table, f, ensure_ascii=False, indent=2)

    def 添加基建技能图标(self):
        source_dir = "./ArknightsGameResource/building_skill"
        destination_dir = f"{RESOURCE_ROOT}/ui/public/building_skill"
        os.makedirs(destination_dir, exist_ok=True)
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                if file.endswith(".png"):
                    src_file_path = os.path.join(root, file)
                    dest_file_path = os.path.join(destination_dir, os.path.splitext(file)[0] + ".webp")
                    if not os.path.exists(dest_file_path):
                        with Image.open(src_file_path) as img: img.save(dest_file_path, "webp")

    def 获取加工站配方类别(self):
        配方类别 = {}
        配方数据 = self.基建表.get("workshopFormulas", {})
        for 配方ID, 配方信息 in 配方数据.items():
            物品ID = 配方信息.get("itemId")
            物品名称 = self.物品表["items"].get(物品ID, {}).get("name", 物品ID)
            子类材料 = [self.物品表["items"].get(i["id"], {}).get("name") for i in 配方信息.get("costs")]
            配方类型 = 配方信息.get("formulaType")
            if 物品名称 == "家具零件": 物品名称 += "_" + 子类材料[0]
            配方类别[物品名称] = {
                "tab": formulaType[配方类型],
                "apCost": 配方信息.get("apCost") / 360000,
                "goldCost": 配方信息.get("goldCost"),
                "items": 子类材料,
            }
        with open(f"{RESOURCE_ROOT}/arknights_mower/data/workshop_formula.json", "w", encoding="utf-8") as f:
            json.dump(配方类别, f, ensure_ascii=False, indent=4)

    def generate_version_info(self):
        # current_time = int(time.time())
        version_path = os.path.join(RESOURCE_ROOT, "arknights_mower/data/version.json")
        version_info = {
            "activity": {"name": "未知", "time": 0, "endTime": 0}, 
            "gacha": {"name": "未知", "time": 0, "endTime": 0},
            "last_updated": "",
            "res_version": "",
            "files": {}
        }
        
        if os.path.exists(version_path):
            with open(version_path, "r", encoding="utf-8") as f:
                version_info.update(json.load(f))

        # 获取上游记录（若存在）
        version_upstream_path = os.path.join(project_root, "version")
        if os.path.exists(version_upstream_path):
            with open(version_upstream_path, "r", encoding="utf-8") as f:
                version_info["last_updated"] = f.read().strip()

        # 获取最新活动
        activities = [
            a for a in self.活动表.get("basicInfo", {}).values()
            if not any(key in a.get("type", "") for key in ["CHECKIN", "ONLY", "COLLECTION"])
        ]

        if activities:
            activities.sort(key=lambda x: x.get("startTime", 0), reverse = True)
            
            latest = activities[0]
            version_info["activity"] = {
                "name": latest.get("name"),
                "time": latest.get("startTime"),
                "endTime": latest.get("endTime")
            }
            print(f"当前最新活动：{latest.get('name')}")
                
        
        # 获取最新卡池
        gacha = [
            g for g in self.抽卡表.get("gachaPoolClient", [])
            if not any(key in g.get("gachaPoolName") for key in ["适合多种场合的强力干员"])
        ]

        if gacha:
            gacha.sort(key=lambda x: x.get("openTime", 0), reverse = True)

            latest = gacha[0]
            version_info["gacha"] = {
                "name": latest.get("gachaPoolName"),
                "time": latest.get("openTime"),
                "endTime": latest.get("endTime")
            }
            print(f"当前最新卡池：{latest.get('gachaPoolName')}")

        version_info["files"] = self.generate_md5(RESOURCE_ROOT)
        with open(version_path, "w", encoding="utf-8") as f:
            json.dump(version_info, f, indent=4, ensure_ascii=False)

    def md5_file(self, path, chunk_size=1024 * 1024):
        md5 = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                data = f.read(chunk_size)
                if not data: break
                md5.update(data)
        return md5.hexdigest()

    def generate_md5(self, dir):
        result_md5 = {}
        for base, _, files in os.walk(dir):
            for name in files:
                full = os.path.join(base, name)
                rel = os.path.relpath(full, dir)
                result_md5[rel.replace("\\", "/")] = self.md5_file(full)
        return result_md5

roomType = {"POWER": "发电站", "DORMITORY": "宿舍", "MANUFACTURE": "制造站", "MEETING": "会客室", 
            "WORKSHOP": "加工站", "TRADING": "贸易站", "HIRE": "人力办公室", "TRAINING": "训练室", "CONTROL": "中枢"}
formulaType = {"F_SKILL": "技巧概要", "F_ASC": "芯片", "F_BUILDING": "基建材料", "F_EVOLVE": "精英材料"}

if __name__ == "__main__":
    数据处理器 = Arknights数据处理器()
    
    if args.local_only:
        print("====== 进入本地专项模式 ======")
        数据处理器.load_recruit_template()
        数据处理器.load_recruit_tag()
        数据处理器.训练在房间内的干员名的模型()
        数据处理器.训练选中的干员名的模型()
        数据处理器.训练训练室干员名的模型()
        print("本地专项训练完毕。")
    else:
        数据处理器.添加物品()
        数据处理器.添加干员()
        数据处理器.读取卡池()
        数据处理器.读取活动关卡()
        数据处理器.批量训练并保存扫仓库模型()
        数据处理器.训练在房间内的干员名的模型()
        数据处理器.训练选中的干员名的模型()
        数据处理器.训练训练室干员名的模型()
        数据处理器.auto_fight_avatar()
        数据处理器.获得干员基建描述()
        数据处理器.buff转换()
        数据处理器.添加基建技能图标()
        数据处理器.load_recruit_resource()
        数据处理器.获取加工站配方类别()
        数据处理器.generate_version_info()
        print("全量资源生成执行完毕。")