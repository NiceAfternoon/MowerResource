import cv2
import numpy as np
from typing import Union

Image = np.ndarray  # RGB 图像
GrayImage = np.ndarray  # 灰度图像

def loadimg(filename: str, gray: bool = False) -> Union[Image, GrayImage]:
    """
    从文件加载图像。
    使用 np.fromfile 以支持包含中文的长路径。
    """
    img_data = np.fromfile(filename, dtype=np.uint8)
    if gray:
        # 加载为灰度图
        return cv2.imdecode(img_data, cv2.IMREAD_GRAYSCALE)
    else:
        # 加载为 BGR 并转换为 RGB
        img = cv2.imdecode(img_data, cv2.IMREAD_COLOR)
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

def thres2(img: GrayImage, thresh: int) -> GrayImage:
    """
    图像二值化处理。
    """
    _, ret = cv2.threshold(img, thresh, 255, cv2.THRESH_BINARY)
    return ret