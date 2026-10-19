"""OCR 抽象层。

优先级：RapidOCR（识别中文最好）> Windows 内置 OCR（零下载）> Tesseract > 手动。

只想跑通流程、暂时不想装模型的话，可以设成 manual：程序会在需要读数时
弹出一张裁剪图让你手输一次，至少把"决策 + 点击"整条链路跑通。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

from .vision import parse_number


@dataclass
class OcrLine:
    text: str
    conf: float
    box: Tuple[int, int, int, int]  # x, y, w, h


class BaseOcr:
    name = "base"

    def available(self) -> bool:
        return False

    def recognize(self, img: Image.Image) -> List[OcrLine]:
        raise NotImplementedError


class RapidOcr(BaseOcr):
    """RapidOCR 两种包都兼容：
    - rapidocr_onnxruntime（旧，Python < 3.13）
    - rapidocr 3.x（新，Python 3.13+，需配合 onnxruntime）
    """

    name = "rapidocr"

    def __init__(self):
        self.api = ""
        # rapidocr 3.x 的默认参数是"通用场景大图"取向，对屏幕小字非常不友好：
        #   * Det.limit_type=min / 736  —— 会把短边放大到 736，600x220 的面板被拉成
        #     2004x736（31 倍像素），单次识别要 4~9 秒，这才是"读得慢"的根因；
        #   * intra_op_num_threads=-1   —— 线程数给得不到位，实测比显式 6 线程慢 2.6 倍；
        #   * use_cls=True              —— 每个文字框都跑一次方向分类，屏幕截图永远是正向，
        #     纯浪费。
        # 改成：不放大（limit_type=max）、按 CPU 核数开线程、关掉方向分类。
        params = {
            "Det.limit_type": "max",
            # 检测按 640 宽跑（约 0.25 秒）；识别用的裁剪框仍然从原图（2 倍放大）取，
            # 所以小字不会糊。再往上调只会让检测变慢，数字并没读得更准。
            "Det.limit_side_len": 640,
            "Global.use_cls": False,
            "EngineConfig.onnxruntime.intra_op_num_threads": max(1, min(8, os.cpu_count() or 4)),
        }
        try:
            from rapidocr_onnxruntime import RapidOCR as _R  # type: ignore

            try:
                self.engine = _R(params=params)
            except TypeError:
                self.engine = _R()
            self.api = "v1"
        except Exception:
            from rapidocr import RapidOCR as _R  # type: ignore

            try:
                self.engine = _R(params=params)
            except Exception:
                self.engine = _R()
            self.api = "v3"
        # rapidocr 3.x 会往控制台刷 INFO 日志，压掉以免挡住决策输出
        import logging

        for name in ("RapidOCR", "rapidocr"):
            logging.getLogger(name).setLevel(logging.ERROR)

    def available(self) -> bool:
        return bool(self.api)

    def recognize(self, img: Image.Image) -> List[OcrLine]:
        arr = np.asarray(img.convert("RGB"))
        out: List[OcrLine] = []
        if self.api == "v1":
            res, _ = self.engine(arr)
            for item in res or []:
                box, text, score = item[0], item[1], float(item[2])
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                out.append(OcrLine(text, score, (int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys)))))
            return out

        res = self.engine(arr)
        txts = getattr(res, "txts", None) or []
        boxes = getattr(res, "boxes", None)
        scores = getattr(res, "scores", None)
        for i, text in enumerate(txts):
            box = boxes[i] if boxes is not None else None
            score = float(scores[i]) if scores is not None else 0.9
            if box is None:
                out.append(OcrLine(text, score, (0, 0, 0, 0)))
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append(OcrLine(text, score, (int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys)))))
        return out


class WindowsOcr(BaseOcr):
    name = "windows"

    def __init__(self):
        self._mod = None
        for mod_name in ("winsdk", "winrt"):
            try:
                __import__(mod_name)
                self._mod = mod_name
                break
            except Exception:
                continue
        if self._mod is None:
            raise RuntimeError("未安装 winsdk / winrt")

    def available(self) -> bool:
        return self._mod is not None

    def recognize(self, img: Image.Image) -> List[OcrLine]:
        if self._mod == "winsdk":
            from winsdk.windows.globalization import Language
            from winsdk.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
            from winsdk.windows.media.ocr import OcrEngine
            from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
        else:
            from winrt.windows.globalization import Language
            from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        async def run() -> List[OcrLine]:
            rgb = img.convert("RGBA")
            w, h = rgb.size
            stream = InMemoryRandomAccessStream()
            writer = DataWriter(stream.get_output_stream_at(0))
            writer.write_bytes(bytearray(rgb.tobytes()))
            await writer.store_async()
            await writer.flush_async()
            stream.seek(0)

            from winrt.windows.graphics.imaging import BitmapDecoder  # type: ignore
            decoder = await BitmapDecoder.create_async(stream)
            bmp = await decoder.get_software_bitmap_async()
            engine = OcrEngine.try_create_from_language(Language("zh-Hans")) or OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                return []
            result = await engine.recognize_async(bmp)
            lines: List[OcrLine] = []
            for ln in result.lines:
                text = "".join(w_.text for w_ in ln.words)
                words = list(ln.words)
                if not words:
                    continue
                x = int(min(w_.bounding_rect.x for w_ in words))
                y = int(min(w_.bounding_rect.y for w_ in words))
                x2 = int(max(w_.bounding_rect.x + w_.bounding_rect.width for w_ in words))
                y2 = int(max(w_.bounding_rect.y + w_.bounding_rect.height for w_ in words))
                lines.append(OcrLine(text, 0.8, (x, y, x2 - x, y2 - y)))
            return lines

        return asyncio.run(run())


class TesseractOcr(BaseOcr):
    name = "tesseract"

    def __init__(self, exe: Optional[str] = None):
        self.exe = exe or "tesseract"

    def available(self) -> bool:
        try:
            subprocess.run([self.exe, "--version"], capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def recognize(self, img: Image.Image) -> List[OcrLine]:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        img.save(path)
        try:
            r = subprocess.run(
                [self.exe, path, "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                capture_output=True, timeout=30,
            )
            text = r.stdout.decode("utf-8", "ignore")
            return [OcrLine(line.strip(), 0.6, (0, 0, 0, 0)) for line in text.splitlines() if line.strip()]
        finally:
            os.unlink(path)


class ManualOcr(BaseOcr):
    name = "manual"

    def available(self) -> bool:
        return True

    def recognize(self, img: Image.Image) -> List[OcrLine]:
        try:
            img.show()
        except Exception:
            pass
        try:
            s = input("  [手输] 请填写上图内容（回车跳过）: ").strip()
        except EOFError:
            s = ""
        return [OcrLine(s, 1.0, (0, 0, 0, 0))] if s else []


def get_ocr(prefer: str = "auto", manual_fallback: bool = True) -> BaseOcr:
    order = [prefer] if prefer not in ("auto", "") else ["rapidocr", "windows", "tesseract"]
    for name in order:
        try:
            if name == "rapidocr":
                o = RapidOcr()
            elif name == "windows":
                o = WindowsOcr()
            elif name == "tesseract":
                o = TesseractOcr()
            elif name == "manual":
                o = ManualOcr()
            else:
                continue
            if o.available():
                return o
        except Exception as e:
            print(f"  [ocr] {name} 不可用：{e}")
    if manual_fallback:
        return ManualOcr()
    raise RuntimeError("没有可用的 OCR 后端")


class Reader:
    """面向业务的读取器：读文本 / 读数字。"""

    def __init__(self, backend: Optional[BaseOcr] = None, upscale: float = 2.0):
        self.backend = backend or get_ocr()
        self.upscale = upscale

    @property
    def name(self) -> str:
        return self.backend.name

    def _prep(self, img: Image.Image) -> Image.Image:
        if self.upscale != 1.0:
            img = img.resize((int(img.width * self.upscale), int(img.height * self.upscale)), Image.LANCZOS)
        return img

    def text(self, img: Image.Image) -> str:
        return " ".join(l.text for l in self.backend.recognize(self._prep(img)))

    def number(self, img: Image.Image) -> Optional[float]:
        txt = self.text(img)
        return parse_number(txt)
