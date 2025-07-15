import math

'''
task 1.1
Реализуйте алгоритм, определяющий все ли символы в строке
встречаются только один раз. А если запрещено использование
дополнительных структур данных?
'''

def task1_1(string):
    for i in range(len(string)):
        for j in range(i+1, len(string)):
            if string[i] == string[j]:
                return False
    return True

'''
task 1.2
Для двух строк напишите метод, определяющий, является 
ли одна строка перестановкой другой
'''

def task1_2(string_1, string_2):
    string_1 = string_1.lower().replace(" ", "")
    string_2 = string_2.lower().replace(" ", "")

    if len(string_1) != len(string_2):
        return False

    s1 = {}
    for i in string_1:
        if i in s1:
            s1[i] += 1
        else:
            s1[i] = 1
    s2 = {}
    for j in string_2:
        if j in s2:
            s2[j] += 1
        else:
            s2[j] = 1
    for symb, count in s1.items():
        if s2[symb] != count:
            return False       
    return True

'''
task 1.3
Напишите метод, заменяющий все пробелы в строке символами
'%20'. Можете считать, что длина строки позволяет сохранить
дополнительные символы, а фактическая длина строки известна
заранее
'''

def task1_3(string_1):
    string_2 = string_1.strip().replace(" ", "%20")
    return string_2

'''
task 1.4
Напишите функцию, которая проверяет, является ли заданная 
строка перестановкой палиндрома.
'''

def task1_4(string_1):
    string_1 = string_1.lower().replace(" ", "")
    s1 = {}
    for i in string_1:
        if i in s1:
            s1[i] += 1
        else:
            s1[i] = 1       
    middle = len(string_1)%2
    for symb, count in s1.items():
        if s1[symb]%2==1:
            middle-=1
            if middle < 0:
                return False     
    return True

'''
task 1.5
Существуют три вида модифицирующих операций со строками: вставка 
символа, удаление символа и замена символа. Напишите функцию, 
которая проверяет, находятся ли две строки на расстоянии одной 
модификации или нуля модификаций
'''

def task1_5(string_1, string_2):
    if abs(len(string_1) - len(string_2))>1:
        return False
    mod_count = 0
    i = 0
    j = 0
    while (min(i,j) < min(len(string_1), len(string_2))):
        if string_1[i] != string_2[j]:
            mod_count+=1
            if mod_count>1:
                return False
            if len(string_1) > len(string_2):
                i+=1
            elif len(string_1) < len(string_2):
                j+=1
        i+=1
        j+=1
    return True

'''
task 1.6
Реализуйте метод для выполнения простейшего сжатия строк с использованием
счетчика повторяющихся символов. Например строка aabcccccaaa превращается
в a2b1c5a3. Если сжатая строка не становится короче исходной, то метод
возвращает исходную строку. Предполагается, что строка состоит только из 
букв верхнего и нижнего регистра a-z
'''

def task1_6(string):
     new_string = ""
     symb_counter = 1
     for i in range(1, len(string)):
         if string[i]==string[i-1]:
             symb_counter+=1
         else:
             new_string = new_string + string[i-1] + str(symb_counter)
             symb_counter = 1
         
     if string[-1] != string[-2]:
         symb_counter=1 
     new_string = new_string + string[-1] + str(symb_counter)
     if len(string) > len(new_string):
         return new_string
     else: 
         return string
        
'''
task 1.7
Имеется изображение, представленное матрицей NxN. Каждый пиксель
представлен 4 байтами. Напишите метод для поворота изображения на
90 градусов. Удастся ли вам выполнить эту операцию "на месте"
'''

def task1_7(image):
    for x in range(len(image[0])):
        for y in range(len(image[0])):
            pass

if __name__ == "__main__":
    print("\n task 1.1")
    print(task1_1("aaaaa"))
    print(task1_1("Ab cde"))

    print("\n task 1.2")
    print(task1_2("aaaaa", "Ab cde"))
    print(task1_2("Ab cde", "bcdea"))

    print("\n task 1.3")
    print("Ab cde")
    print(task1_3("Ab cde"))

    print("\n task 1.4")
    print(task1_4("aaaaa"))
    print(task1_4("bcdea"))
    print(task1_4("abcdcba"))

    print("\n task 1.5")
    print(task1_5("pale", "ple"))
    print(task1_5("pales", "pale"))
    print(task1_5("pale", "bale"))
    print(task1_5("pale", "bake"))
    print(task1_5("pale", "bakeeeee"))

    print("\n task 1.6")
    print(task1_6("aabcccccaaa"))

    matrix = [[1, 0, 0],[1, 1, 0],[1, 1, 1]]
    matrix = [[1, 1, 1],[1, 1, 0],[1, 0, 0]]