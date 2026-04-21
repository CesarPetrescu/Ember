package com.cesarpetrescu.ember.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

val AppBg = Color(0xFF0C0C0E)
val AppBgAlt = Color(0xFF101014)
val Panel = Color(0xFF17181B)
val PanelAlt = Color(0xFF1F2024)
val Border = Color(0xFF2C2D32)
val TextMain = Color(0xFFF2F2F2)
val TextDim = Color(0xFFC9C9CC)
val Muted = Color(0xFF82828A)
val Accent = Color(0xFFE38B56)
val AccentFg = Color(0xFF1A1208)
val Error = Color(0xFFE07878)
val Success = Color(0xFF7EC699)
val CodeBg = Color(0xFF101012)

private val EmberColors =
  darkColorScheme(
    primary = Accent,
    onPrimary = AccentFg,
    secondary = TextDim,
    background = AppBg,
    onBackground = TextMain,
    surface = Panel,
    onSurface = TextMain,
    surfaceVariant = PanelAlt,
    onSurfaceVariant = TextDim,
    outline = Border,
    error = Error,
  )

private val EmberTypography =
  Typography(
    bodyLarge = TextStyle(fontFamily = FontFamily.Default, fontSize = 16.sp, lineHeight = 22.sp, letterSpacing = 0.sp),
    bodyMedium = TextStyle(fontFamily = FontFamily.Default, fontSize = 14.sp, lineHeight = 20.sp, letterSpacing = 0.sp),
    labelMedium = TextStyle(fontFamily = FontFamily.Default, fontSize = 12.sp, lineHeight = 16.sp, letterSpacing = 0.sp),
    titleLarge = TextStyle(fontFamily = FontFamily.Default, fontSize = 20.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.sp),
    titleMedium = TextStyle(fontFamily = FontFamily.Default, fontSize = 16.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.sp),
  )

@Composable
fun EmberTheme(content: @Composable () -> Unit) {
  MaterialTheme(colorScheme = EmberColors, typography = EmberTypography, content = content)
}
