"""Интерактивный подбор координат QR (qr-code_110) на OTC-скрине.

Запуск: `python place_qr_otc.py` (в обычном терминале — нужен интерактивный ввод).

Берёт pictures/shot_1m_otc.png, накладывает pictures/qr-code_110.png в текущих
координатах, сохраняет превью pictures/screenshot_otc_preview.png.

Команды: 'x y' — переместить QR; Enter — выход.
Подобранные координаты перенеси в настройки наложения OTC-QR.
"""
from pathlib import Path

from PIL import Image

SHOT = Path("pictures/shot_1m_otc.png")
QR_PATH = "pictures/qr-code_110.png"
OUT = Path("pictures/screenshot_otc_preview.png")

# Стартовая координата (левый-верхний угол QR). Подбери и перенеси в настройки.
QR_START = (1320, 82)


def _load_overlay(path: str):
    raw = Image.open(path)
    mask = raw.split()[-1] if raw.mode in ("RGBA", "LA") else None
    return raw.convert("RGBA"), mask


def render(pos: tuple[int, int], qr, qr_mask) -> None:
    """Накладывает QR на свежую копию OTC-скрина и сохраняет в OUT."""
    with Image.open(SHOT) as base:
        base = base.convert("RGBA") if base.mode != "RGBA" else base.copy()
        base.paste(qr, pos, mask=qr_mask)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        base.save(OUT)


def main():
    if not SHOT.exists():
        print(f"❌ Нет {SHOT}. Сначала прогони OTC-бот, чтобы скрин сохранился.")
        return
    if not Path(QR_PATH).exists():
        print(f"❌ Нет {QR_PATH}.")
        return

    qr, qr_mask = _load_overlay(QR_PATH)
    base_w, base_h = Image.open(SHOT).size
    print(f"скрин: {SHOT} {base_w}x{base_h}")
    print(f"QR:    {QR_PATH} {qr.size[0]}x{qr.size[1]}")
    print(f"out:   {OUT}")

    pos = list(QR_START)
    render(tuple(pos), qr, qr_mask)
    print(f"стартовый превью → {OUT}")

    while True:
        qw, qh = qr.size
        print(f"\nтекущие: x={pos[0]} y={pos[1]}   (правый-нижний: {pos[0] + qw}, {pos[1] + qh})")
        raw = input("новые 'x y' | Enter — выйти: ").strip()
        if not raw:
            print("выход.")
            return
        parts = raw.replace(",", " ").split()
        if len(parts) != 2:
            print("нужно два числа, например: 0 640")
            continue
        try:
            x, y = int(parts[0]), int(parts[1])
        except ValueError:
            print("оба значения должны быть целыми.")
            continue
        pos = [x, y]
        render(tuple(pos), qr, qr_mask)
        print(f"сохранено → {OUT}   (QR={tuple(pos)})")


if __name__ == "__main__":
    main()
