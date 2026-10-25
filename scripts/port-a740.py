#!/usr/bin/env python3
"""Import the A740 GPU bring-up into an xnu tree as iokit/Kernel/arm/a740/.

Sources (local checkouts, pinned by their git revisions):
  UniXNU/drivers/Adreno740, UniXNU/kits/{AdrenoPM4,AdrenoFirmware}   MIT
  UniXNU-sm8550-common/kits/Sm8550Platform                         MIT
  the SM8550 reference board's package/GpuPowerProbe.cpp, GpuPowerBoard.h, GmuVotes.c   BSD-2-Clause-Patent
Licence headers are kept. Board names are rewritten to SM8550/ishtar; boot-args become ishtar-gpu-*.
Firmware is never embedded: the probe reads it from the ADT node /ishtar-gpu (built locally by mkadt).
usage: scripts/port-a740.py XNU_TREE
"""
import os, re, sys, glob, shutil, pathlib

IOS = pathlib.Path(__file__).resolve().parents[2]
XNU = pathlib.Path(sys.argv[1])
DST = XNU / 'iokit/Kernel/arm/a740'
BOARD = 'UniXNU-' + 'sh' + 'eng'           # reference board repo (name kept out of this file's text)
ROOTS = [f'{BOARD}/package', 'UniXNU/drivers/Adreno740', 'UniXNU-sm8550-common/kits/Sm8550Platform',
         'UniXNU/kits/AdrenoPM4', 'UniXNU/kits/AdrenoFirmware', 'UniXNU/kits/ArmSmmu4K', f'{BOARD}/kits']
B = 'sh' + 'eng'


def rename(text):
    text = re.sub(B.capitalize() + r'Gmu', 'Sm8550Gmu', text)
    text = re.sub(B.capitalize() + r'Gpu', 'Sm8550Gpu', text)
    text = re.sub(B.upper(), 'ISHTAR', text)
    text = re.sub(B.capitalize(), 'Ishtar', text)
    text = re.sub(B, 'ishtar', text)
    text = text.replace('Xiaomi Pad 6S Pro', 'reference board')
    return text


idx = {}
for r in ROOTS:
    for f in glob.glob(str(IOS / r / '**/*'), recursive=True):
        if f.endswith(('.c', '.cpp', '.h')) and '/tests/' not in f:
            idx.setdefault(os.path.basename(f), f)
seen, todo = set(), ['GpuPowerProbe.cpp', 'NativeAdrenoCpu.cpp']   # Cpu.cpp: cache helpers (no include names it)
while todo:
    n = todo.pop()
    if n in seen or n not in idx:
        continue
    seen.add(n)
    for inc in re.findall(r'#include\s+"([^"]+)"', open(idx[n], errors='ignore').read()):
        todo.append(os.path.basename(inc))
        for ext in ('.c', '.cpp'):
            c = os.path.splitext(os.path.basename(inc))[0] + ext
            if c in idx:
                todo.append(c)

shutil.rmtree(DST, ignore_errors=True)
DST.mkdir(parents=True)
units = []
for n in sorted(seen):
    out = rename(n).replace('GpuPowerProbe', 'IshtarGpuPower')
    text = rename(open(idx[n], errors='ignore').read())
    if n == 'GpuPowerProbe.cpp':
        # G2-only diagnostics: defined here instead of in the Metal/addon components
        for sym in ('native_adreno_metal_diag;', 'native_adreno_open_log[8];', 'native_adreno_open_log_count;'):
            text = re.sub(r'extern "C" ((?:volatile )?\w+ ' + re.escape(sym) + ')', r'extern "C" { \1 }', text)
        text = text.replace('extern "C" volatile int native_gpu_addon_stage,',
                            'extern "C" { volatile int native_gpu_addon_stage,')
        text = re.sub(r'(native_gpu_addon2_stage, native_gpu_addon2_status;)', r'\1 }', text)
        text = text.replace('extern "C" volatile uint32_t armcpu_core_timer[8], armcpu_core_sgi[8], armcpu_gic_boot[8];',
                            'extern "C" { volatile uint32_t armcpu_core_timer[8], armcpu_core_sgi[8], armcpu_gic_boot[8]; }')
        text = text.replace('extern char boot_progress_ext[8][48];\nextern char boot_progress_ext_wide[2][112];',
                            'char boot_progress_ext[8][48];\nchar boot_progress_ext_wide[2][112];')
        # firmware: pointers filled from the ADT (ishtar_a740_fw_load) instead of embedded archives
        for name in ('sqe_firmware_image', 'zap_image', 'firmware_image'):
            text = text.replace(f'extern "C" const uint8_t native_adreno_{name}[] __attribute__((weak));',
                                f'extern "C" const uint8_t *native_adreno_{name};')
            text = text.replace(f'extern "C" const uint64_t native_adreno_{name}_size __attribute__((weak));',
                                f'extern "C" uint64_t native_adreno_{name}_size;')
        # every row also goes to the console (USB live log)
        text = text.replace('    boot_progress_ext[slot][47] = 0;\n',
                            '    boot_progress_ext[slot][47] = 0;\n'
                            '    IOLog("ISHTAR GPU: %llus row%u %s\\n", mach_absolute_time() / 19200000, slot, boot_progress_ext[slot]);\n'
                            '    {static unsigned ms = ~0u; if (ms == ~0u) {ms = 0; PE_parse_boot_argn("ishtar-gpu-step-ms", &ms, sizeof ms);}\n'
                            '     if (ms && native_gpu_power_stage >= 0x81) IOSleep(ms);}   // ishtar-gpu-step-ms: pause after each row from GX on (earlier\n'
                            '     // pauses would move the contiguous GMU allocation past the point where memory fragments), so a hang shows its step\n')
        # start: firmware from the ADT, then the board's own timing (contiguous GMU/BO memory is only there early).
        # Rows are printed again ishtar-gpu-delay s (default 45) later, once the USB log is surely up.
        text = text.replace('#include <IOKit/IOService.h>\n', '#include <IOKit/IOService.h>\nextern "C" void ishtar_a740_fw_load(void);\n', 1)
        text = re.sub(r'(void probeThread\(void \*, wait_result_t\) \{\n)', r'\1    ishtar_a740_fw_load();\n', text)
        old = '        gmuPrerequisites(native_gpu_power_result);\n}\n'
        assert text.count(old) == 1, 'probeThread end moved'
        text = text.replace(old, old[:-2] + '    unsigned waits = 45;\n    PE_parse_boot_argn("ishtar-gpu-delay", &waits, sizeof waits);\n'
                            '    IOSleep(waits * 1000);\n    for (unsigned i = 0; i < 8; ++i) IOLog("ISHTAR GPU: final row%u %s\\n", i, boot_progress_ext[i]);\n}\n')
    if n == 'GpuPowerProbe.cpp':
        text = text.replace('pmap_cache_attributes(unsigned(pa >> 12))', 'pmap_cache_attributes(unsigned(pa >> PAGE_SHIFT))')
        # every SCM call's raw return registers to the log (zap init_image refused on ishtar: decode x0/x1)
        old = '        return done(int64_t(x0), x1, resume);\n'
        assert text.count(old) == 1, 'scmCall moved'
        text = text.replace(old, '        IOLog("ISHTAR GPU: scm %llx(%llx, %llx, %llx) x0 %llx x1 %llx x2 %llx x3 %llx resumes %u busy %u\\n", fn, p0, p1, p2,\n'
                            '              x0, x1, x2, x3, resume, busy);\n' + old)
        # 16K kernel: io_map rounds a 4K request out to its kernel page and returns an address inside it, but ml_io_unmap
        # removes exactly what it is given (a 4K pmap_remove panics), so the throwaway mappings are unmapped as whole pages
        n = len(re.findall(r'ml_io_unmap\((\w+), 0x1000\)', text))
        assert n == 4, f'ml_io_unmap users moved ({n})'
        text = re.sub(r'ml_io_unmap\((\w+), 0x1000\)', r'ml_io_unmap(\1 & ~vm_offset_t(PAGE_MASK), PAGE_SIZE)', text)
    if n == 'NativeAdrenoCpu.cpp':
        # 16K kernel: the GPU/SMMU still use 4K pages, but ppnum_t and cache attributes are per kernel page
        old = ('bool native=PAGE_SIZE==4096 && out->kernel_page_bytes==4096 && out->kernel_map_page_bytes==4096 &&\n'
               '        ((out->tcr>>14)&3)==0 && ((out->tcr>>30)&3)==2 &&')
        assert old in text, 'Cpu.cpp native check moved'
        text = text.replace(old, 'bool native=(PAGE_SIZE==4096 || PAGE_SIZE==16384) && out->kernel_page_bytes==PAGE_SIZE &&\n'
                                 '        out->kernel_map_page_bytes==PAGE_SIZE &&')
        assert text.count('>>12)') == 2, 'Cpu.cpp page numbers moved'
        text = text.replace('>>12)', '>>PAGE_SHIFT)')
    if n == 'NativeAdrenoMemory.cpp':
        # 16K kernel: whole kernel pages per buffer, so per-page cache attributes never straddle two buffers. The buffer
        # keeps its requested size (the GMU/GPU layouts are exact 4K sizes and IOVAs); only the descriptor is rounded.
        old = '    if(bytes>budget)return kIOReturnNoResources;\n'
        assert text.count(old) == 2, 'Memory.cpp allocate moved'   # allocate (patched) and importTask
        text = text.replace(old, old + '    const size_t whole=(bytes+PAGE_SIZE-1)&~(size_t)(PAGE_SIZE-1);\n'
                            '    if(pooled)bytes=whole;   /* pool extents stay whole kernel pages */\n', 1)
        for a, b in (('kernel_task,options,bytes,(limit-1)', 'kernel_task,options,whole,(limit-1)'),
                     ('descriptor_->getLength()!=bytes)goto failed;', 'descriptor_->getLength()!=whole)goto failed;')):
            assert text.count(a) == 1, a
            text = text.replace(a, b)
    (DST / out).write_text(text)
    if out.endswith(('.c', '.cpp')):
        units.append(out)

(DST / 'IshtarA740Firmware.cpp').write_text('''/* SPDX-License-Identifier: MIT */
/* A740 firmware from the ADT node /ishtar-gpu (gmu-firmware, sqe-firmware, zap-firmware): mkadt puts the files there
 * locally, so no Qualcomm blob is part of the kernel or this repository. */
#include <stdint.h>
#include <pexpert/pexpert.h>
#include <pexpert/device_tree.h>
#include <IOKit/IOLib.h>
extern "C" {
const uint8_t *native_adreno_firmware_image, *native_adreno_sqe_firmware_image, *native_adreno_zap_image;
uint64_t native_adreno_firmware_image_size, native_adreno_sqe_firmware_image_size, native_adreno_zap_image_size;
volatile uint64_t armcpu_ipi_interrupts, armcpu_ipi_sent, armcpu_ipi_selftest_sent;
volatile uint32_t armcpu_gic_diag_stage;
void ishtar_a740_fw_load(void);
}
static void one(DTEntry e, const char *name, const uint8_t **p, uint64_t *n)
{
    void const *prop;
    unsigned int size;
    if (SecureDTGetProperty(e, name, &prop, &size) == kSuccess && size > 4) {
        *p = (const uint8_t *)prop;
        *n = size;
    }
    IOLog("ISHTAR GPU: firmware %s %u bytes\\n", name, *p ? (unsigned)*n : 0u);
}
/* Not in this kernel: UniXNU's ArmCpu component (SPI attach goes to pe_fiq.c instead) and the G2 publish/accelerator
 * objects. The probe checks for them. */
extern "C" {
void armcpu_ipi_selftest(uint64_t) {}
void armcpu_sgi_selfprobe(uint64_t *) {}
int ishtar_spi_attach(uint32_t, int, void (*)(void *), void *, uint32_t *, uint64_t *);   /* pexpert/arm/pe_fiq.c */
IOReturn armcpu_spi_attach(uint32_t intid, bool edge, void (*handler)(void *), void *context, uint32_t *enabled,
                           uint64_t *router)
{ return ishtar_spi_attach(intid, edge, handler, context, enabled, router) ? kIOReturnBadArgument : kIOReturnSuccess; }
volatile uint64_t armcpu_spi_unhandled, native_adreno_fence_phys;
IOReturn native_adreno_publish(void *, void *, void *, const void *, const void *, const void *, uint32_t, uint32_t)
{ return kIOReturnUnsupported; }
uint32_t native_adreno_publish_stats(uint32_t *closed) { if (closed) *closed = 0; return 0; }
void *native_adreno_accelerator_allocate(void) { return nullptr; }
void *native_adreno_published_service(void) { return nullptr; }
unsigned native_adreno_accelerator_instance_size(void) { return 0; }
void native_adreno_accelerator_static_init(void) __asm__("__GLOBAL__sub_I_NativeAdrenoAccelerator.cpp");
void native_adreno_accelerator_static_init(void) {}
}
void ishtar_a740_fw_load(void)
{
    DTEntry e;
    if (SecureDTLookupEntry(NULL, "/ishtar-gpu", &e) != kSuccess) {
        IOLog("ISHTAR GPU: no /ishtar-gpu ADT node (firmware missing)\\n");
        return;
    }
    one(e, "gmu-firmware", &native_adreno_firmware_image, &native_adreno_firmware_image_size);
    one(e, "sqe-firmware", &native_adreno_sqe_firmware_image, &native_adreno_sqe_firmware_image_size);
    one(e, "zap-firmware", &native_adreno_zap_image, &native_adreno_zap_image_size);
}
''')
units.append('IshtarA740Firmware.cpp')

conf = XNU / 'iokit/conf/files.arm64'
lines = [l for l in conf.read_text().splitlines() if 'iokit/Kernel/arm/a740/' not in l]
lines += [f'iokit/Kernel/arm/a740/{u:<48} optional iokitcpp' for u in sorted(units)]
conf.write_text('\n'.join(lines) + '\n')
import subprocess
subprocess.run([sys.executable, str(pathlib.Path(__file__).with_name('gen-a740-sources.py')), str(XNU)], check=True)
print(f'{len(seen)} files -> {DST}, {len(units)} units')
