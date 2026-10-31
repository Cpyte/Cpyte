class Operation:
    def apply(value: int) -> int:
        return value + 1

class Doubler(Operation):
    def apply(value: int) -> int:
        return value * 2

public main() -> int:
    Operation* increment_op = new Operation()
    Operation* double_op = (Operation*)new Doubler()
    Operation* current = increment_op
    int total = 0
    int i = 0
    while i < 1000000:
        if i % 2 == 0:
            current = increment_op
        else:
            current = double_op
        total = total + current.apply(i % 100)
        i = i + 1
    print(total)
    return 0
