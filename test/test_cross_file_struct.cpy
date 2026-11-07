# Cross-file struct access: the consumer imports only the middle module, which
# itself imports the module that defines `Vec2`. Field access, by-value struct
# parameters/returns and the type name must all resolve transitively.

import "test_xfile_mid.cpy"

public main() -> int:
    Vec2 v = scaled(2, 3, 4)
    print(v.x)
    print(v.y)
    print(norm1(v))

    Vec2 w = make_vec2(10, 20)
    print(vec2_dot(v, w))
    print(w.x + w.y)
    return 0
