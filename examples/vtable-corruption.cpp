/*
 * vtable-corruption.cpp
 *
 * Crash type : Access violation (0xC0000005) — call through corrupted vtable
 * Mechanism  : A polymorphic object (Dog*) is deleted, its memory is
 *              overwritten with 0xDE bytes, and then a virtual method is
 *              called through the stale pointer.  The vtable pointer
 *              (first 8 bytes of the object) now reads 0xDEDEDEDEDEDEDEDE,
 *              so the indirect call dereferences unmapped memory.
 *
 * What to look for in WinDbg:
 *   - AV on a call/jmp through a bogus address like 0xDEDEDEDE…
 *   - Object memory filled with 0xDE (visible via dqs / db)
 *   - Vtable pointer no longer pointing to valid .rdata
 */
#include <stdio.h>
#include <string.h>
#include "crashdump.h"

class Animal
{
public:
    virtual const char *species() const = 0;
    virtual void speak() const = 0;
    virtual ~Animal() {}
};

class Dog : public Animal
{
    char name_[32];
public:
    Dog(const char *name)
    {
        strcpy_s(name_, sizeof(name_), name);
        printf("  Dog(\"%s\") constructed at %p\n", name_, this);
    }
    ~Dog() override
    {
        printf("  Dog(\"%s\") destroyed   at %p\n", name_, this);
    }
    const char *species() const override { return "Canis familiaris"; }
    void speak() const override
    {
        printf("  %s says: Woof!\n", name_);
    }
};

int main(void)
{
    EnableCrashDumps();

    printf("=== Vtable Corruption Demo ===\n\n");

    Animal *pet = new Dog("Buddy");
    pet->speak();

    printf("\n  Deleting object...\n");
    size_t obj_size = sizeof(Dog);
    void *raw = (void *)pet;
    delete pet;

    /* Overwrite the freed memory (including the vtable pointer)
       with a distinctive poison byte. */
    memset(raw, 0xDE, obj_size);
    printf("  Memory at %p filled with 0xDE (%zu bytes)\n\n", raw, obj_size);

    /* BUG: call virtual method on destroyed + overwritten object.
       pet->speak() does:
         1. Read vtable ptr from *pet  -> 0xDEDEDEDEDEDEDEDE
         2. Index into vtable for speak -> AV (unmapped address)         */
    printf("  Calling speak() through stale pointer...\n");
    pet->speak();

    return 0;
}
