package com.cesarpetrescu.ember.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.assertFalse
import org.junit.Test
import kotlin.random.Random

class ModelsTest {
  @Test
  fun tokenCounts_areFormattedForTheContextBar() {
    assertEquals("0", formatTokenCount(0))
    assertEquals("999", formatTokenCount(999))
    assertEquals("1k", formatTokenCount(1_000))
    assertEquals("1.2k", formatTokenCount(1_234))
    assertEquals("1M", formatTokenCount(1_000_000))
    assertEquals("12.3M", formatTokenCount(12_345_678))
  }

  @Test
  fun contextRemaining_accountsForBaselineTokens() {
    assertEquals(100, percentOfContextRemaining(12_000, 272_000))
    assertEquals(50, percentOfContextRemaining(142_000, 272_000))
    assertEquals(0, percentOfContextRemaining(272_000, 272_000))
    assertEquals(100, percentOfContextRemaining(5_000, 272_000))
  }

  @Test
  fun gpt55_isAvailableAsTheDefaultModel() {
    assertEquals("gpt-5.5", DefaultModel)
    assertEquals(listOf("low", "medium", "high", "xhigh"), ModelSpecs[DefaultModel])
    assertEquals(272_000, ContextWindows[DefaultModel])
    assertTrue(MaxContextWindows["gpt-5.5"] == 1_000_000)
  }

  @Test
  fun everySupportedModelHasAMatchingContextWindowAndEffortModes() {
    assertTrue(ModelSpecs.isNotEmpty())
    ModelSpecs.forEach { (model, efforts) ->
      assertFalse("Model $model should expose effort options", efforts.isEmpty())
      assertTrue("Context window missing for $model", ContextWindows.containsKey(model))
      assertTrue("gpt-5.5 context window should be stable", if (model == "gpt-5.5") ContextWindows[model] == 272_000 else true)
      assertEquals("xhigh", efforts.last())
    }
  }

  @Test
  fun percentOfContextRemaining_isAlwaysInRangeForRandomTraffic() {
    val baseline = BaselineTokens
    repeat(300) {
      val total = Random.nextInt(-100_000, 600_000)
      val window = listOf(128_000, 272_000, 1_000_000).random()
      val percent = percentOfContextRemaining(total, window)
      assertTrue(percent in 0..100)
      if (total <= baseline || total <= 0) {
        assertEquals(100, percent)
      } else if (total >= window) {
        assertTrue("Window overuse should drop usage to 0 or less", percent <= 0)
      }
    }
  }
}
