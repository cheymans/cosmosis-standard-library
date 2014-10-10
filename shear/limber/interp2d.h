#ifndef _H_INTERP2D
#define _H_INTERP2D

#include <gsl/gsl_spline.h>
#include <gsl/gsl_interp.h>

typedef struct Interpolator2D{
  double *x1;
  double *x2;
  double *y;
  int N1;
  int N2;
  double *y1;
  gsl_spline *x1dir_spline;
  gsl_interp_accel *x1dir_accel;
  gsl_spline **x2dir_splines;
  gsl_interp_accel **x2dir_accels;
} Interpolator2D;

typedef double (*interp2d_modifier_function)(double,double,double,void*);

Interpolator2D * init_interp_2d(double *x1, double *x2, double *y, int N1, int N2, const gsl_interp_type *T);
Interpolator2D * init_interp_2d_akima(double *x1, double *x2, double *y, int N1, int N2);
Interpolator2D * init_interp_2d_grid(double *x1, double *x2, double **y, int N1, int N2, const gsl_interp_type *T);
Interpolator2D * init_interp_2d_akima_grid(double *x1, double *x2, double **y, int N1, int N2);

void destroy_interp_2d(Interpolator2D * interp2d);
double interp_2d(double x1, double x2, Interpolator2D * interp2d);
Interpolator2D * load_interp_2d(const char * filename, int N1, int N2);


#endif