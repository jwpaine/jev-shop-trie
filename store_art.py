"""Flat SVG illustrations for the Northwoods catalog: one drawing per product type, in the product's color.

    art(kind, hex, plaid=False) -> an <svg> string with a 0 0 100 100 viewBox

Every drawing shares one recipe: the body in the product color, trim in a
darker step of it, highlights in a lighter step, and a soft ground shadow.
"""

import colorsys


def shade(hex_: str, by: float) -> str:
    """Lighter (by > 0) or darker (by < 0) step of a color, in HLS lightness."""
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l + by)), s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _plaid(uid: str, c: str) -> str:
    dark, light = shade(c, -0.18), shade(c, 0.22)
    return (f'<pattern id="pl{uid}" width="16" height="16" patternUnits="userSpaceOnUse">'
            f'<rect width="16" height="16" fill="{c}"/><rect width="16" height="5" y="5" fill="{dark}" opacity=".55"/>'
            f'<rect width="5" height="16" x="5" fill="{dark}" opacity=".55"/>'
            f'<rect width="16" height="1.2" y="13" fill="{light}" opacity=".7"/>'
            f'<rect width="1.2" height="16" x="13" fill="{light}" opacity=".7"/></pattern>')


def _boot(c, d, l):
    return (f'<path d="M30 18h26v38c0 4 3 6 7 7l15 4c5 1 8 5 8 10v5H22V24c0-3 3-6 8-6z" fill="{c}"/>'
            f'<path d="M22 79h64v7H22z" fill="{d}"/><path d="M22 72h64v3H22z" fill="{l}" opacity=".5"/>'
            f'<path d="M30 18h26v8H30z" fill="{d}"/>'
            + "".join(f'<path d="M38 {y}h12" stroke="{l}" stroke-width="2.5" stroke-linecap="round"/>' for y in (34, 42, 50)))


def _moccasin(c, d, l):
    return (f'<path d="M12 70c0-12 12-20 30-22l28-3c12-1 20 6 20 14v6c0 5-4 8-9 8H18c-4 0-6-1-6-3z" fill="{c}"/>'
            f'<path d="M40 52c4-6 14-9 24-8 8 1 12 5 12 9" fill="none" stroke="{d}" stroke-width="3"/>'
            f'<path d="M12 73h78v5c0 2-2 4-5 4H17c-3 0-5-2-5-4z" fill="{d}"/>'
            f'<path d="M30 50c6 2 12 2 18 0" stroke="{l}" stroke-width="3" fill="none" stroke-linecap="round"/>')


def _sneaker(c, d, l):
    return (f'<path d="M14 66c0-10 6-18 14-22l16-8c4 6 10 9 18 10l18 3c7 1 12 7 12 13v4H14z" fill="{c}"/>'
            f'<path d="M12 66h82v7c0 3-3 6-6 6H18c-3 0-6-3-6-6z" fill="{l}"/>'
            f'<path d="M12 75h82" stroke="{d}" stroke-width="4"/>'
            + "".join(f'<path d="M{x} {y}l7-4" stroke="{l}" stroke-width="2.5" stroke-linecap="round"/>'
                      for x, y in ((42, 46), (48, 49), (54, 52))))


def _jacket(c, d, l):
    return (f'<path d="M36 14l14 6 14-6 20 8 8 50-12 2-4-34v52H32V40l-4 34-12-2 8-50z" fill="{c}"/>'
            f'<path d="M50 20v66" stroke="{d}" stroke-width="2.5"/>'
            f'<path d="M36 14l14 10 14-10" fill="none" stroke="{d}" stroke-width="3"/>'
            f'<path d="M36 52h9M55 52h9" stroke="{d}" stroke-width="2.5" stroke-linecap="round"/>'
            f'<path d="M24 22l-4 26" stroke="{l}" stroke-width="2" opacity=".6"/>')


def _vest(c, d, l):
    return (f'<path d="M34 14l16 8 16-8 6 10c-6 4-8 10-8 18v44H36V42c0-8-2-14-8-18z" fill="{c}"/>'
            f'<path d="M50 22v64" stroke="{d}" stroke-width="2.5"/>'
            + "".join(f'<path d="M36 {y}h28" stroke="{d}" stroke-width="1.5" opacity=".7"/>' for y in (44, 56, 68, 80))
            + f'<path d="M34 14l16 10 16-10" fill="none" stroke="{l}" stroke-width="3"/>')


def _pullover(c, d, l):
    return (f'<path d="M36 18l14 4 14-4 22 8 6 46-12 2-6-30v42H32V44l-6 30-12-2 6-46z" fill="{c}"/>'
            f'<path d="M42 18l8 14 8-14" fill="{d}"/><path d="M50 22v14" stroke="{l}" stroke-width="2"/>'
            f'<path d="M32 80h36" stroke="{d}" stroke-width="5"/>'
            f'<path d="M16 70l12 2M84 70l-12 2" stroke="{d}" stroke-width="4"/>')


def _shirt(c, d, l):
    return (f'<path d="M36 16l14 6 14-6 20 8 6 40-12 2-4-26v46H32V40l-4 26-12-2 6-40z" fill="{c}"/>'
            f'<path d="M36 16l8 12 6-6 6 6 8-12" fill="{d}"/><path d="M50 22v64" stroke="{d}" stroke-width="2"/>'
            + "".join(f'<circle cx="53" cy="{y}" r="1.6" fill="{l}"/>' for y in (34, 46, 58, 70))
            + f'<path d="M36 42h9v8h-9z" fill="none" stroke="{d}" stroke-width="2"/>')


def _tee(c, d, l):
    return (f'<path d="M34 18c4 4 10 6 16 6s12-2 16-6l22 10-8 16-10-4v46H30V40l-10 4-8-16z" fill="{c}"/>'
            f'<path d="M34 18c4 6 10 8 16 8s12-2 16-8" fill="none" stroke="{d}" stroke-width="3"/>'
            f'<path d="M58 40h9v9h-9z" fill="none" stroke="{d}" stroke-width="2"/>')


def _beanie(c, d, l):
    return (f'<circle cx="50" cy="16" r="9" fill="{l}"/>'
            f'<path d="M18 62c0-22 14-38 32-38s32 16 32 38z" fill="{c}"/>'
            + "".join(f'<path d="M{x} 30v32" stroke="{d}" stroke-width="2" opacity=".5"/>' for x in (32, 42, 50, 58, 68))
            + f'<rect x="14" y="60" width="72" height="18" rx="5" fill="{d}"/>'
            + "".join(f'<path d="M{x} 62v14" stroke="{c}" stroke-width="2.5"/>' for x in range(20, 84, 7)))


def _gloves(c, d, l):
    def mitt(x):
        return (f'<path d="M{x} 30c0-8 6-12 12-12s12 4 12 12v30h-24z" fill="{c}"/>'
                f'<path d="M{x} 44c-8-2-10 6-6 12l6 6z" fill="{c}"/>'
                f'<rect x="{x - 2}" y="60" width="28" height="16" rx="3" fill="{d}"/>'
                f'<path d="M{x + 4} 32c2-6 8-8 12-6" stroke="{l}" stroke-width="2" fill="none"/>')
    return mitt(22) + mitt(56)


def _backpack(c, d, l):
    return (f'<path d="M38 20c0-6 5-10 12-10s12 4 12 10" fill="none" stroke="{d}" stroke-width="4"/>'
            f'<rect x="24" y="20" width="52" height="66" rx="14" fill="{c}"/>'
            f'<path d="M24 38c8-6 44-6 52 0v6c-8-4-44-4-52 0z" fill="{d}"/>'
            f'<rect x="34" y="58" width="32" height="20" rx="5" fill="{d}"/>'
            f'<path d="M36 64h28" stroke="{l}" stroke-width="2"/>'
            f'<rect x="46" y="48" width="8" height="12" rx="2" fill="{l}"/>')


def _tote(c, d, l):
    return (f'<path d="M34 34c0-14 6-20 16-20s16 6 16 20" fill="none" stroke="{d}" stroke-width="5"/>'
            f'<path d="M18 34h64l-6 52H24z" fill="{c}"/>'
            f'<path d="M18 34h64v8H18z" fill="{d}"/>'
            f'<rect x="36" y="54" width="28" height="16" rx="3" fill="{l}" opacity=".85"/>')


def _tent(c, d, l):
    return (f'<path d="M8 82L50 18l42 64z" fill="{c}"/><path d="M50 18L32 82h36z" fill="{d}"/>'
            f'<path d="M50 40L40 82h20z" fill="{shade(c, -0.32)}"/>'
            f'<path d="M50 12v8" stroke="{d}" stroke-width="2.5"/><path d="M4 82h92" stroke="{d}" stroke-width="3"/>'
            f'<path d="M16 72l20-30" stroke="{l}" stroke-width="2" opacity=".6"/>')


def _sleeping(c, d, l):
    return (f'<path d="M30 14h40c8 0 12 6 12 14v50c0 6-6 10-14 10H32c-8 0-14-4-14-10V28c0-8 4-14 12-14z" fill="{c}"/>'
            f'<ellipse cx="50" cy="28" rx="20" ry="9" fill="{d}"/>'
            + "".join(f'<path d="M20 {y}c20 4 40 4 60 0" fill="none" stroke="{d}" stroke-width="1.8" opacity=".7"/>'
                      for y in (46, 58, 70))
            + f'<path d="M74 34v48" stroke="{l}" stroke-width="2.5"/>')


def _lantern(c, d, l):
    return (f'<path d="M38 16c0-6 24-6 24 0" fill="none" stroke="{d}" stroke-width="4"/>'
            f'<rect x="32" y="16" width="36" height="12" rx="4" fill="{c}"/>'
            f'<rect x="34" y="28" width="32" height="40" rx="6" fill="#fff3c4"/>'
            f'<circle cx="50" cy="48" r="11" fill="#ffd56b"/><circle cx="50" cy="48" r="5" fill="#fff8e0"/>'
            f'<path d="M42 28v40M58 28v40" stroke="{c}" stroke-width="3"/>'
            f'<rect x="30" y="68" width="40" height="14" rx="4" fill="{c}"/><path d="M30 76h40" stroke="{d}" stroke-width="2"/>')


def _chair(c, d, l):
    return (f'<path d="M28 28h44l-4 30H32z" fill="{c}"/><path d="M28 28h44v6H28z" fill="{d}"/>'
            f'<path d="M24 58h52v8H24z" fill="{c}"/>'
            f'<path d="M30 66L22 88M70 66l8 22M30 66l40 22M70 66L30 88" stroke="{d}" stroke-width="3" stroke-linecap="round"/>'
            f'<path d="M20 50h12M68 50h12" stroke="{d}" stroke-width="4" stroke-linecap="round"/>'
            f'<path d="M36 36v16" stroke="{l}" stroke-width="2" opacity=".6"/>')


def _blanket(c, d, l):
    return (f'<path d="M14 30c0-6 4-10 10-10h52c6 0 10 4 10 10v20H14z" fill="{d}"/>'
            f'<path d="M14 44c0-4 3-7 7-7h58c4 0 7 3 7 7v26c0 5-4 8-8 8H22c-4 0-8-3-8-8z" fill="{c}"/>'
            f'<path d="M14 52h72M14 62h72" stroke="{l}" stroke-width="2.5" opacity=".7"/>'
            + "".join(f'<path d="M{x} 78v6" stroke="{d}" stroke-width="2"/>' for x in range(20, 84, 6)))


def _mug(c, d, l):
    return (f'<path d="M68 38h6c8 0 12 6 12 12s-4 12-12 12h-6" fill="none" stroke="{c}" stroke-width="7"/>'
            f'<path d="M22 28h48v46c0 6-5 10-10 10H32c-5 0-10-4-10-10z" fill="{c}"/>'
            f'<ellipse cx="46" cy="28" rx="24" ry="5" fill="{d}"/><ellipse cx="46" cy="28" rx="20" ry="3.5" fill="#6b4226"/>'
            f'<path d="M30 40v30" stroke="{l}" stroke-width="3" opacity=".6" stroke-linecap="round"/>'
            f'<path d="M40 18c-4-6 4-8 0-14M50 18c-4-6 4-8 0-14" stroke="{l}" stroke-width="2" fill="none" opacity=".7"/>')


def _bottle(c, d, l):
    return (f'<rect x="40" y="8" width="20" height="12" rx="3" fill="{d}"/>'
            f'<path d="M36 26c0-4 4-6 8-6h12c4 0 8 2 8 6v58c0 4-3 6-6 6H42c-3 0-6-2-6-6z" fill="{c}"/>'
            f'<path d="M36 44h28v22H36z" fill="{d}" opacity=".55"/>'
            f'<path d="M42 30v52" stroke="{l}" stroke-width="3" opacity=".55" stroke-linecap="round"/>')


DRAW = {"boot": _boot, "moccasin": _moccasin, "sneaker": _sneaker, "jacket": _jacket, "vest": _vest,
        "pullover": _pullover, "shirt": _shirt, "tee": _tee, "beanie": _beanie, "gloves": _gloves,
        "backpack": _backpack, "tote": _tote, "tent": _tent, "sleeping": _sleeping, "lantern": _lantern,
        "chair": _chair, "blanket": _blanket, "mug": _mug, "bottle": _bottle}


def art(kind: str, hex_: str, plaid: bool = False, uid: str = "") -> str:
    """The product's illustration. `uid` keeps pattern ids unique when many share a page."""
    defs, body = "", hex_
    if plaid:
        defs, body = f"<defs>{_plaid(uid, hex_)}</defs>", f"url(#pl{uid})"
    return (f'<svg viewBox="0 0 100 100" role="img" aria-hidden="true">{defs}'
            f'<ellipse cx="50" cy="90" rx="34" ry="4" fill="#000" opacity=".12"/>'
            f'{DRAW[kind](body, shade(hex_, -0.14), shade(hex_, 0.2))}</svg>')
