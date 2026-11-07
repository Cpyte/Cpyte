# Middle module: re-exports Vec2 through its own functions without importing
# the consumer's view of the type. See test/test_cross_file_struct.cpy.

import "test_xfile_vec.cpy"

public def scaled(x: int, y: int, k: int) -> Vec2:
    Vec2 v = make_vec2(x, y)
    v.x = v.x * k
    v.y = v.y * k
    return v

public def norm1(v: Vec2) -> int:
    return v.x + v.y
