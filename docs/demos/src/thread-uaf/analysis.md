### Threads
```
  thread #1: tid=0x3aafc3  idle (watchdog, already returned from pool->close)
* thread #2: tid=0x3aafed  Session::log_request  <-- crashed here
```

### Crash Information
```
Signal: SIGSEGV — write to unmapped memory
Faulting frame: thread-uaf`Session::log_request at thread-uaf.cpp:100:22
```

### Faulting Source Code (worker thread)
```cpp
     99 |     void log_request(const char *path) {
>>> 100 |         request_count++;
    101 |         printf("[session %d] %s  (req #%d)\n", id, path, request_count);
    102 |     }
```

### Where the memory went (watchdog thread, SessionPool::close)
```cpp
    127 |     void close(Session *s) {
    128 |         printf("[pool] closing session %d @ %p\n", s->id, (void*)s);
    129 |         int idx = s->id % 32;
    130 |         slots[idx] = nullptr;
    131 |         delete s;   // <-- BUG: worker still holds a raw pointer to this
    132 |     }
```
