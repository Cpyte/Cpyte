int a = input()

moneys = [1, 2, 5, 10, 20, 50, 100]
n = 6
int count = 0

while a != 0:
    if a >= moneys[n]:
        a -= moneys[n]
        count+=1
    else:
        n -= 1

print(count)
