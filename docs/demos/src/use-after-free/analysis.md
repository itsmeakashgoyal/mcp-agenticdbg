### Crash Information
```
Signal: SIGSEGV — write to unmapped/poisoned memory
Faulting frame: use-after-free`main at use-after-free.cpp:89:30
```

### Faulting Source Code
- **Source:** `use-after-free.cpp` (from debug info)
- **Faulting line:** 89

```cpp
    73 |     close_connection(conn);
    74 |     conn = NULL;
    75 |
    76 |     /* Poison the freed block through the dangling pointer */
    79 |     memset(dangling, 0xAB, sizeof(Connection));
    80 |
    83 |     void *reuse = malloc(sizeof(Connection));
    84 |
    85 |     printf("Accessing freed connection through dangling pointer...\n");
    86 |
    87 |     /* dangling->recv_buffer now reads 0xABABABABABABABAB.
    88 |        Dereferencing that as a pointer -> ACCESS VIOLATION. */
>>> 89 |     dangling->recv_buffer[0] = 999;
```
