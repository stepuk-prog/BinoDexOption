"""Интерактивный подбор координат QR-оверлеев на скрине (FIN: 2 QR, OTC: 1 QR).

Запуск (нужен интерактивный терминал):
    python place_qr.py            # FIN: QR110 + QR85, самый свежий pictures/shot*.png
    python place_qr.py otc        # OTC: QR110, pictures/shot_1m_otc.png

Команды: 'x y' — переместить активный QR; для FIN '110'/'85' — переключить активный; Enter — выход.
Стартовые координаты берутся из settings/screenshot_set.py; подобранные значения перенеси туда же.
"""
import glob
import os
import sys
from pathlib import Path

from PIL import Image

from settings.screenshot_set import qr110_x, qr110_y, qr85_x, qr85_y, otc_qr_x, otc_qr_y

QR110_PATH = "pictures/qr-code_110.png"
QR85_PATH = "pictures/qr-code_85.png"


def _newest_shot() -> Path | None:
    shots = sorted(glob.glob("pictures/shot*.png"), key=os.path.getmtime, reverse=True)
    return Path(shots[0]) if shots else None


def _load_overlay(path: str):
    raw = Image.open(path)
    mask = raw.split()[-1] if raw.mode in ("RGBA", "LA") else None
    return raw.convert("RGBA"), mask


def _config(mode: str):
    """(shot, out, spec): spec = [(key, путь_к_QR, стартовая_координата), ...]."""
    if mode == 'otc':
        return (Path("pictures/shot_1m_otc.png"), Path("pictures/screenshot_otc_preview.png"),
                [('110', QR110_PATH, (otc_qr_x, otc_qr_y))])
    return (_newest_shot(), Path("pictures/screenshot_preview.png"),
            [('110', QR110_PATH, (qr110_x, qr110_y)), ('85', QR85_PATH, (qr85_x, qr85_y))])


def render(shot: Path, out: Path, overlays: dict) -> None:
    """Накладывает все оверлеи на свежую копию скрина и сохраняет в out."""
    with Image.open(shot) as base:
        base = base.convert("RGBA") if base.mode != "RGBA" else base.copy()
        for ov in overlays.values():
            base.paste(ov['img'], tuple(ov['pos']), mask=ov['mask'])
        out.parent.mkdir(parents=True, exist_ok=True)
        base.save(out)


def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else 'fin'
    shot, out, spec = _config(mode)
    if shot is None or not shot.exists():
        print(f"❌ Нет скрина для режима {mode}. Сначала прогони бот, чтобы скрин сохранился.")
        return

    overlays: dict = {}
    for key, path, start in spec:
        if not Path(path).exists():
            print(f"❌ Нет {path}.")
            return
        img, mask = _load_overlay(path)
        overlays[key] = {'pos': list(start), 'img': img, 'mask': mask}

    keys = list(overlays)
    active = keys[0]
    print(f"режим: {mode}   скрин: {shot} {Image.open(shot).size}   out: {out}")
    render(shot, out, overlays)
    print(f"стартовый превью → {out}")

    switch_hint = (" | " + "/".join(keys) + " — переключить") if len(keys) > 1 else ""
    while True:
        ov = overlays[active]
        qw, qh = ov['img'].size
        x, y = ov['pos']
        print(f"\nактивный: QR{active}   x={x} y={y}   (правый-нижний: {x + qw}, {y + qh})")
        raw = input(f"новые 'x y'{switch_hint} | Enter — выйти: ").strip()
        if not raw:
            print("выход.")
            return
        if raw in keys:
            active = raw
            continue
        parts = raw.replace(",", " ").split()
        if len(parts) != 2:
            print("нужно два числа, например: 1320 820")
            continue
        try:
            x, y = int(parts[0]), int(parts[1])
        except ValueError:
            print("оба значения должны быть целыми.")
            continue
        overlays[active]['pos'] = [x, y]
        render(shot, out, overlays)
        coords = ", ".join(f"QR{k}={tuple(v['pos'])}" for k, v in overlays.items())
        print(f"сохранено → {out}   ({coords})")


if __name__ == "__main__":
    main()
