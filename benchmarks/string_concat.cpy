def main() -> int:
    s = ""
    for i in range(10000):
        s = s + "hello"
    print(len(s))
    return 0
