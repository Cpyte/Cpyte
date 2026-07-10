import "examples/cpy_struct_lib.cpy"

def main():
    Point p = make_point(10, 20)
    print_point(p)
    print(p.x)
    return p.x