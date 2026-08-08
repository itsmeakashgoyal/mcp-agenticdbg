### Crash Information
```
Signal: SIGABRT — libmalloc detected corrupted heap metadata
Faulting frame: double-free`main at double-free.cpp:59:5
```

### Backtrace
```
abort
  ↳ malloc_vreport
    ↳ malloc_zone_error
      ↳ free_tiny_botch
        ↳ main  (double-free.cpp:59)
```

### Faulting Source Code
- **Source:** `double-free.cpp` (from debug info)
- **Faulting line:** 59

```cpp
    35 |     free(data);
    36 |     printf("freed      data = %p  (first time — ok)\n", data);
    39 |     char *other = (char *)malloc(BLOCK_SIZE);
    43 |     /* BUG: free the original pointer again. */
    46 |     free(data);
    52 |     for (int i = 0; i < 10000; i++) {
    54 |         void *p = malloc(BLOCK_SIZE);
    56 |         free(p);
    57 |     }
>>> 59 |     free(other);
```
