struct Box:
    int w
    int h
    int d

public box_volume(a: int, w: int, h: int, d: int) -> int:
    Box b
    b.w = w
    b.h = h
    b.d = d
    return b.w * b.h * b.d
