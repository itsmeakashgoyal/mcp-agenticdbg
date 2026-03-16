/*
 * multi-inheritance-crash.cpp
 *
 * Crash type  : SIGSEGV — virtual dispatch through the wrong vtable slot
 *               caused by an incorrect C-style downcast across a
 *               multiple-inheritance hierarchy
 * Mechanism   : ISerializable and IProcessor are independent pure-virtual
 *               bases.  ConcreteProcessor inherits from both.  A factory
 *               function returns a ConcreteProcessor* as ISerializable*.
 *               A consumer incorrectly casts that ISerializable* back to
 *               IProcessor* using a C-style cast, which does NOT adjust the
 *               pointer — it lands on the ISerializable vtable segment
 *               inside the object instead of the IProcessor vtable segment.
 *               Calling process() through this mis-adjusted pointer
 *               dispatches to the wrong function pointer, typically
 *               executing garbage or a completely unrelated virtual method.
 *
 * Complexity  : The cast looks superficially correct; both interfaces are
 *               implemented by the same concrete class.  The bug only
 *               manifests at runtime with a confusing crash inside
 *               an unrelated method or at an invalid address.  Multiple
 *               layers of registry / factory abstraction hide the cast site.
 *
 * What to look for in GDB:
 *   - Crash inside IProcessor::process() dispatch — address looks like a
 *     valid vtable entry for ISerializable (wrong interface)
 *   - `info frame` / `print this` shows a pointer into the middle of the
 *     ConcreteProcessor object, not its base address
 *   - `print *(ConcreteProcessor*)correct_ptr` reveals the actual object
 *   - Root cause: C-style cast in ComponentRegistry::get_processor() does
 *     not adjust the pointer; use dynamic_cast or static_cast instead
 *
 * Fix hint:
 *   - Replace the C-style cast `(IProcessor*)serial_ptr` with
 *     `dynamic_cast<IProcessor*>(serial_ptr)` and check for nullptr, OR
 *   - Change the registry to store IProcessor* directly.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <vector>
#include <string>
#include "crashdump.h"

// ---------------------------------------------------------------------------
// Interface A: ISerializable
// ---------------------------------------------------------------------------
class ISerializable {
public:
    virtual ~ISerializable() {}
    virtual std::string serialize() const = 0;
    virtual bool        deserialize(const std::string &data) = 0;
    virtual const char *schema_name() const = 0;
};

// ---------------------------------------------------------------------------
// Interface B: IProcessor
// ---------------------------------------------------------------------------
class IProcessor {
public:
    virtual ~IProcessor() {}
    virtual void        process(const void *input, size_t len) = 0;
    virtual const char *processor_name() const = 0;
    virtual int         result_code() const = 0;
};

// ---------------------------------------------------------------------------
// Concrete class inheriting both interfaces
// ---------------------------------------------------------------------------
class ConcreteProcessor : public ISerializable, public IProcessor {
    char   name_[64];
    int    result_;
    char   last_schema_[32];
public:
    explicit ConcreteProcessor(const char *name) : result_(0) {
        strncpy(name_, name, sizeof(name_) - 1);
        name_[sizeof(name_)-1] = '\0';
        strncpy(last_schema_, "v1.0", sizeof(last_schema_)-1);
        printf("[ConcreteProcessor] '%s' constructed @ %p\n", name_, (void*)this);
        printf("  ISerializable sub-obj @ %p\n", (void*)static_cast<ISerializable*>(this));
        printf("  IProcessor    sub-obj @ %p\n", (void*)static_cast<IProcessor*>(this));
    }

    // ISerializable
    std::string serialize() const override {
        return std::string("{\"name\":\"") + name_ + "\",\"result\":" +
               std::to_string(result_) + "}";
    }
    bool deserialize(const std::string &data) override {
        printf("[%s] deserialize(%s)\n", name_, data.c_str());
        return true;
    }
    const char *schema_name() const override { return last_schema_; }

    // IProcessor
    void process(const void *input, size_t len) override {
        const char *s = (const char *)input;
        printf("[%s] processing %zu bytes: %.32s...\n", name_, len, s);
        result_++;
    }
    const char *processor_name() const override { return name_; }
    int         result_code() const override { return result_; }
};

// ---------------------------------------------------------------------------
// Registry: stores components as ISerializable* (wrong abstraction level)
// ---------------------------------------------------------------------------
struct ComponentRegistry {
    struct Entry {
        char           key[64];
        ISerializable *component;    // stored as ISerializable*
    };

    std::vector<Entry> entries;

    void register_component(const char *key, ISerializable *c) {
        Entry e;
        strncpy(e.key, key, sizeof(e.key)-1);
        e.key[sizeof(e.key)-1] = '\0';
        e.component = c;
        entries.push_back(e);
        printf("[registry] registered '%s' (ISerializable@ %p)\n", key, (void*)c);
    }

    IProcessor *get_processor(const char *key) {
        for (auto &e : entries) {
            if (strcmp(e.key, key) == 0) {
                // BUG: C-style cast does NOT adjust the pointer for multiple
                // inheritance.  The IProcessor sub-object lives at a different
                // offset than ISerializable inside ConcreteProcessor.
                // This returns the ISerializable vtable address instead of
                // the IProcessor vtable address → virtual dispatch goes wrong.
                return (IProcessor *)(e.component);   // should be dynamic_cast
            }
        }
        return nullptr;
    }
};

// ---------------------------------------------------------------------------
// Pipeline stages
// ---------------------------------------------------------------------------

static void run_pipeline(ComponentRegistry &reg, const char *component_key,
                         const char *payload) {
    printf("\n[pipeline] looking up processor '%s'\n", component_key);
    IProcessor *proc = reg.get_processor(component_key);
    if (!proc) {
        fprintf(stderr, "[pipeline] processor not found: %s\n", component_key);
        return;
    }
    printf("[pipeline] got IProcessor* = %p\n", (void*)proc);

    // Verify name through vtable dispatch — may already be wrong
    printf("[pipeline] name = '%s'\n", proc->processor_name()); // may crash here

    // Now process — virtual dispatch through misaligned vtable
    proc->process(payload, strlen(payload));                    // or crash here

    printf("[pipeline] result_code = %d\n", proc->result_code());
}

static void run_serialization_check(ComponentRegistry &reg, const char *key) {
    for (auto &e : reg.entries) {
        if (strcmp(e.key, key) == 0) {
            printf("[serial] schema=%s  data=%s\n",
                   e.component->schema_name(),
                   e.component->serialize().c_str());
            return;
        }
    }
}

// ---------------------------------------------------------------------------

int main(void) {
    EnableCrashDumps();
    printf("=== Multiple-Inheritance Vtable Crash Demo ===\n\n");

    ComponentRegistry reg;

    // Create components
    ConcreteProcessor *cp1 = new ConcreteProcessor("ImageResizer");
    ConcreteProcessor *cp2 = new ConcreteProcessor("AudioNormaliser");

    // Register as ISerializable* (stored at ISerializable sub-object address)
    reg.register_component("image",  static_cast<ISerializable*>(cp1));
    reg.register_component("audio",  static_cast<ISerializable*>(cp2));

    // Serialization works fine (using correct ISerializable* pointer)
    printf("\n[main] serialization check...\n");
    run_serialization_check(reg, "image");

    // Processing: registry returns a mis-cast pointer → vtable dispatch crash
    printf("\n[main] running pipeline...\n");
    run_pipeline(reg, "image",
                 "RAW_IMAGE_DATA:width=1920,height=1080,fmt=RGBA32");
    run_pipeline(reg, "audio",
                 "PCM_DATA:rate=44100,channels=2,samples=4096");

    printf("\n[main] done\n");
    delete cp1;
    delete cp2;
    return 0;
}
