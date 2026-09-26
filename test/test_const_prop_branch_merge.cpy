def branch_value(flag int) -> int:
    int value = 0
    if flag == 0:
        value = 10
    else:
        value = 20
    return value + 1

def switch_value(flag int) -> int:
    int value = 0
    switch flag:
        case 0:
            value = 10
        case 1:
            value = 20
        default:
            value = 30
    return value + 1

def main() -> int:
    print(branch_value(0))
    print(branch_value(1))
    print(switch_value(0))
    print(switch_value(1))
    print(switch_value(2))
    return 0
