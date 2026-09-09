def guard(pad int) -> decorated:
    if pad >= 0:
        code()
        int a0 = args[0]
        int a1 = args[1]
        result = result + a0 + a1
        print(func_name)
    else:
        skip = true

@guard(5)
def add(a int, b int) -> int:
    return a * 10 + b

@guard(-1)
def skipcompute() -> int:
    return 999

print(add(2, 3))
print(skipcompute())