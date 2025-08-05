from tasks import add
result = add.delay(4, 6)
result.ready()      # -> True/False
result.get()        # -> 10 (после выполнения)