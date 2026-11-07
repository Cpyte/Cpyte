# Type definitions used through a chain of imports (cross-file struct access).

struct Vec2:
    int x
    int y

public def make_vec2(x: int, y: int) -> Vec2:
    Vec2 v
    v.x = x
    v.y = y
    return v

public def vec2_dot(a: Vec2, b: Vec2) -> int:
    return a.x * b.x + a.y * b.y
