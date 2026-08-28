#pragma once

namespace doc {

/**
 * Computes the quotient of two integers.
 *
 * Performs integer division and reports failure via an exception. This second
 * paragraph is detail text.
 *
 * @param numerator the value to divide
 * @param denominator the divisor; must not be zero
 * @return the integer quotient
 * @retval 0 when the numerator is zero
 * @throws std::domain_error if @p denominator is zero
 * @note rounding follows truncation toward zero
 * @warning undefined for INT_MIN / -1
 * @since 1.2
 * @see multiply
 * @author Ada
 */
int divide(int numerator, int denominator);

/// @brief Multiplies two values.
/// @tparam T an arithmetic type
/// @param a first factor
/// @param b second factor
/// @return the product
template <typename T>
T multiply(T a, T b);

/// @deprecated use divide instead
int old_divide(int a, int b);

/**
 * Squares a value.
 * @code
 *   int y = square(3); // y == 9
 *   if (y > 0) {
 *     return y;
 *   }
 * @endcode
 * Prose written after the block stays after it.
 */
int square(int x);

/**
 * @brief Fills a buffer.
 * @param[out] result where the answer is written
 * @param[in] value the input value
 * @param[in,out] scratch reused working storage
 * @param plain a parameter with no direction attribute
 */
void fill(int* result, int value, int* scratch, int plain);

/**
 * @brief Emphasis and HTML reach the reader.
 *
 * Inline markup: @b bold, @e italic, @c code and @p value. A cross-reference
 * to @ref divide "the divide function" too.
 *
 * HTML such as <b>tags</b> and <ul><li>list items</li></ul> is preserved.
 */
int emphasize(int value);

/**
 * @brief Sorts a range in place.
 * @details The long story: a stable insertion sort, chosen for short ranges.
 * @par Rationale
 * Short ranges dominate the call sites.
 * @remark the comparison must be a strict weak ordering
 * @invariant the range stays a permutation of its input
 * @todo switch to a merge sort above a length threshold
 * @bug loops forever on a comparator that is not irreflexive
 * @author Ada
 * @version 2.1
 * @date 2026-08-01
 */
void sort_range(int* first, int* last);

/**
 * @brief Sorts a range into a new buffer.
 * @copydoc sort_range
 */
void sort_copy(const int* first, const int* last, int* out);

/** @copydoc doc::sort_range(int*, int*) */
void sort_again(int* first, int* last);

//! @brief Spins the widget.
//!
//! @param turns how many turns to spin
void spin(int turns);

/**
 * @overload void spin(int turns)
 * @brief Spins the widget twice.
 */
void spin_twice();

/**
 * @brief Copies bytes between two buffers, stopping at the
 *        first null byte.
 *
 * @param destination the buffer written to, which must be large
 *                    enough to hold the whole string
 * @return the number of bytes copied
 */
int copy_string(char* destination, const char* source);

}  // namespace doc
