# Class `geo::Circle`

**Inherits from** {cpp:any}`geo::Shape`.

**Related functions** {cpp:any}`geo::scale`.

```{cpp:class} geo::Circle : public Shape

A circle.

Defined by its radius.

:::{note}
The radius must be positive.
:::
```

## Method `geo::Circle::area`

```{cpp:function} double geo::Circle::area() const

Compute the area.

:returns: the area in square units.
```

## Field `geo::Circle::radius`

```{cpp:member} double geo::Circle::radius

The radius of the circle.
```
