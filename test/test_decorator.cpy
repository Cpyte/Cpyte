def timer(n int) -> decorated:
    code()
    result = result + n

@timer(10)
def compute() -> int:
    return 42

print(compute())
