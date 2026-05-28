"""
Скрипт для подбора позиции водяного знака OTC.
Запуск: python test_watermark_otc.py
"""

from PIL import Image

# ============================================================
# НАСТРОЙКИ - меняй здесь
# ============================================================

SOURCE = "pictures/shot_1m_otc.png"
OUTPUT = "pictures/screenshot_1m_otc.png"
WATERMARK = "pictures/water_otc.png"

MOVE_X = 898   # позиция по горизонтали
MOVE_Y = 800   # позиция по вертикали

# ============================================================


def main():
    print(f"Позиция: x={MOVE_X}, y={MOVE_Y}")

    img = Image.open(SOURCE)
    print(f"Исходник: {SOURCE} ({img.size[0]}x{img.size[1]})")

    water = Image.open(WATERMARK)
    print(f"Водяной знак: {WATERMARK} ({water.size[0]}x{water.size[1]})")

    img.paste(water, (MOVE_X, MOVE_Y), mask=water)
    img.save(OUTPUT)
    print(f"Сохранено: {OUTPUT}")

    import subprocess
    subprocess.Popen(['xdg-open', OUTPUT])


if __name__ == "__main__":
    main()
