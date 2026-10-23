"""Execute the native stop functions with a deterministic late capture shutdown.

The fake libobs worker cannot disconnect until the caller yields at its native
event wait (or returns). This reproduces the unsafe interleaving without sleeps,
Docker, OBS or FFmpeg: freeing the encoder before disconnection is observable.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


HARNESS = r"""
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdlib.h>
#include <assert.h>
#include <stdio.h>
#define UNUSED_PARAMETER(x) (void)(x)
#define LOG_ERROR 1
#define OS_EVENT_TYPE_MANUAL 1
typedef struct { pthread_mutex_t mutex; pthread_cond_t cond; bool set; } os_event_t;
typedef int calldata_t;
typedef int signal_handler_t;
typedef struct { atomic_bool active; } obs_output_t;
struct ffmpeg_output { obs_output_t *output; bool active; };
static pthread_mutex_t gate_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t gate_cond = PTHREAD_COND_INITIALIZER;
static bool gate, connected, early_free;
static int frees;
static pthread_t worker;
static void (*deactivated)(void *, calldata_t *);
static void *callback_data;

static int os_event_init(os_event_t **e, int type)
{
    UNUSED_PARAMETER(type);
    *e = calloc(1, sizeof(**e));
    assert(*e);
    pthread_mutex_init(&(*e)->mutex, NULL);
    pthread_cond_init(&(*e)->cond, NULL);
    return 0;
}
static void os_event_signal(os_event_t *e)
{
    pthread_mutex_lock(&e->mutex);
    e->set = true;
    pthread_cond_signal(&e->cond);
    pthread_mutex_unlock(&e->mutex);
}
static void open_gate(void)
{
    pthread_mutex_lock(&gate_mutex);
    gate = true;
    pthread_cond_signal(&gate_cond);
    pthread_mutex_unlock(&gate_mutex);
}
static void os_event_wait(os_event_t *e)
{
    open_gate();
    pthread_mutex_lock(&e->mutex);
    while (!e->set)
        pthread_cond_wait(&e->cond, &e->mutex);
    pthread_mutex_unlock(&e->mutex);
}
static void os_event_destroy(os_event_t *e)
{
    pthread_cond_destroy(&e->cond);
    pthread_mutex_destroy(&e->mutex);
    free(e);
}
static signal_handler_t *obs_output_get_signal_handler(obs_output_t *o)
{
    UNUSED_PARAMETER(o);
    return NULL;
}
static void signal_handler_connect(signal_handler_t *h, const char *n,
                                   void (*cb)(void *, calldata_t *), void *data)
{
    UNUSED_PARAMETER(h); UNUSED_PARAMETER(n);
    deactivated = cb;
    callback_data = data;
}
static void signal_handler_disconnect(signal_handler_t *h, const char *n,
                                      void (*cb)(void *, calldata_t *), void *data)
{
    UNUSED_PARAMETER(h); UNUSED_PARAMETER(n);
    UNUSED_PARAMETER(cb); UNUSED_PARAMETER(data);
}
static bool obs_output_active(obs_output_t *o) { return atomic_load(&o->active); }
static void *disconnect_capture(void *data)
{
    obs_output_t *o = data;
    pthread_mutex_lock(&gate_mutex);
    while (!gate)
        pthread_cond_wait(&gate_cond, &gate_mutex);
    connected = false;
    pthread_mutex_unlock(&gate_mutex);
    if (deactivated)
        deactivated(callback_data, NULL);
    atomic_store(&o->active, false);
    return NULL;
}
static void obs_output_end_data_capture(obs_output_t *o)
{
    assert(pthread_create(&worker, NULL, disconnect_capture, o) == 0);
}
static void ffmpeg_deactivate(struct ffmpeg_output *o)
{
    UNUSED_PARAMETER(o);
    pthread_mutex_lock(&gate_mutex);
    early_free = connected;
    frees++;
    pthread_mutex_unlock(&gate_mutex);
}
static void blog(int level, const char *message)
{
    UNUSED_PARAMETER(level);
    fprintf(stderr, "%s", message);
}
"""

RUNNER = r"""
static bool run(void (*stop)(void *))
{
    obs_output_t native;
    atomic_init(&native.active, true);
    struct ffmpeg_output out = { &native, true };
    gate = false; connected = true; early_free = false; frees = 0;
    deactivated = NULL; callback_data = NULL;
    stop(&out);
    open_gate();
    pthread_join(worker, NULL);
    assert(frees == 1);
    return early_free;
}
int main(void)
{
    assert(run(upstream_stop));
    assert(!run(patched_stop));
    return 0;
}
"""


class CaptureTeardownTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cc"), "native regression needs a C compiler on the test host")
    def test_encoder_free_waits_for_capture_disconnect(self):
        patch = (Path(__file__).resolve().parents[1] / "patches/ffmpeg-capture-teardown.patch").read_text()
        lines = patch.splitlines()
        upstream = "\n".join(line[1:] for line in lines if line.startswith("-") and not line.startswith("---"))
        patched = "\n".join(line[1:] for line in lines if line.startswith("+") and not line.startswith("+++"))
        upstream = upstream.replace("ffmpeg_output_full_stop", "upstream_stop")
        patched = patched.replace("ffmpeg_output_full_stop", "patched_stop")
        with tempfile.TemporaryDirectory() as tmp:
            binary = str(Path(tmp) / "teardown")
            compiled = subprocess.run(
                ["cc", "-std=c17", "-pthread", "-x", "c", "-o", binary, "-"],
                input=HARNESS + upstream + "\n" + patched + RUNNER, text=True, capture_output=True,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([binary], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
