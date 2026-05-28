"""Интерактивный подбор координат QR-оверлеев (QR110 + QR85) на скрине.

Запуск: `python place_qr.py` (в обычном терминале — нужен интерактивный ввод).

Поведение (по образцу boevого пайплайна apps.app.screenshot):
1. Берёт самый свежий pictures/shot*.png БЕЗ crop.
2. Накладывает pictures/qr-code_110.png + pictures/qr-code_85.png в текущих
   координатах и сохраняет превью pictures/screenshot_preview.png.
3. Просит ввести `x y` для активного QR, либо `110`/`85` чтобы переключить
   активный, либо Enter — выход.

Когда координаты подобраны — перенеси их в настройки наложения QR.
"""
import glob
import os
from pathlib import Path

from PIL import Image

QR110_PATH = "pictures/qr-code_110.png"
QR85_PATH = "pictures/qr-code_85.png"
OUT = Path("pictures/screenshot_preview.png")

# Стартовые координаты (левый-верхний угол оверлея). Подбери и перенеси в настройки.
QR110_START = (0, 640)
QR85_START = (1348, 850)


def _newest_shot() -> Path | None:
    shots = sorted(glob.glob("pictures/shot*.png"), key=os.path.getmtime, reverse=True)
    return Path(shots[0]) if shots else None


def _load_overlay(path: str):
    raw = Image.open(path)
    mask = raw.split()[-1] if raw.mode in ("RGBA", "LA") else None
    return raw.convert("RGBA"), mask


def render(shot: Path, pos110, pos85, qr110, m110, qr85, m85) -> None:
    """Накладывает оба QR на свежую копию скрина и сохраняет в OUT."""
    with Image.open(shot) as base:
        base = base.convert("RGBA") if base.mode != "RGBA" else base.copy()
        base.paste(qr110, pos110, mask=m110)
        base.paste(qr85, pos85, mask=m85)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        base.save(OUT)


def main():
    shot = _newest_shot()
    if shot is None:
        print("❌ Нет pictures/shot*.png. Сначала прогони бот, чтобы скрин сохранился.")
        return
    for p in (QR110_PATH, QR85_PATH):
        if not Path(p).exists():
            print(f"❌ Нет {p}.")
            return

    qr110, m110 = _load_overlay(QR110_PATH)
    qr85, m85 = _load_overlay(QR85_PATH)
    base_w, base_h = Image.open(shot).size
    print(f"скрин: {shot} {base_w}x{base_h}")
    print(f"QR110: {QR110_PATH} {qr110.size[0]}x{qr110.size[1]}")
    print(f"QR85:  {QR85_PATH} {qr85.size[0]}x{qr85.size[1]}")
    print(f"out:   {OUT}")

    pos = {110: list(QR110_START), 85: list(QR85_START)}
    active = 110
    # Стартовый превью, чтобы видеть исходное положение.
    render(shot, tuple(pos[110]), tuple(pos[85]), qr110, m110, qr85, m85)
    print(f"стартовый превью → {OUT}")

    while True:
        cur_x, cur_y = pos[active]
        qw, qh = (qr110 if active == 110 else qr85).size
        print(f"\nактивный: QR{active}   текущие: x={cur_x} y={cur_y}   "
              f"(правый-нижний: {cur_x + qw}, {cur_y + qh})")
        raw = input("новые 'x y' | '110'/'85' — переключить | Enter — выйти: ").strip()
        if not raw:
            print("выход.")
            return
        if raw in ('110', '85'):
            active = int(raw)
            continue
        parts = raw.replace(",", " ").split()
        if len(parts) != 2:
            print("нужно два числа, например: 1335 670")
            continue
        try:
            x, y = int(parts[0]), int(parts[1])
        except ValueError:
            print("оба значения должны быть целыми.")
            continue
        pos[active] = [x, y]
        render(shot, tuple(pos[110]), tuple(pos[85]), qr110, m110, qr85, m85)
        print(f"сохранено → {OUT}   (QR110={tuple(pos[110])}, QR85={tuple(pos[85])})")


if __name__ == "__main__":
    main()
