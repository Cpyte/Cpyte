class Operation:
    def apply(self, value):
        return value + 1


class Doubler(Operation):
    def apply(self, value):
        return value * 2


increment_op = Operation()
double_op = Doubler()
total = 0
for i in range(1_000_000):
    current = increment_op if i % 2 == 0 else double_op
    total += current.apply(i % 100)
print(total)
