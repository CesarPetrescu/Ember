package com.cesarpetrescu.ember.data

import org.junit.Assert.assertEquals
import org.junit.Test

class ModelsTest {
  @Test
  fun tokenCounts_areFormattedForTheContextBar() {
    assertEquals("999", formatTokenCount(999))
    assertEquals("1.2k", formatTokenCount(1_234))
    assertEquals("1M", formatTokenCount(1_000_000))
  }

  @Test
  fun contextRemaining_accountsForBaselineTokens() {
    assertEquals(100, percentOfContextRemaining(12_000, 272_000))
    assertEquals(50, percentOfContextRemaining(142_000, 272_000))
    assertEquals(0, percentOfContextRemaining(272_000, 272_000))
  }
}
