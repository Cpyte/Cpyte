struct Point:
    int x
    int y

public main() -> int:
    Point p
    p.x = 10
    p.y = 20
    print(p.x + p.y)
    return 0
