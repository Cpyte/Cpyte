struct Box:
    size_t len

public box_len(a: int, v: size_t) -> size_t:
    Box b
    b.len = v
    return b.len
