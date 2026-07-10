struct Point:
    int x
    int y

public def make_point(x int, y int) -> Point:
    Point p
    p.x = x
    p.y = y
    return p

public def print_point(p Point):
    print(p.x)
    print(p.y)
